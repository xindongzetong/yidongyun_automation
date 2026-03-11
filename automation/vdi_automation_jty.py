#!/usr/bin/env python3
"""
VDI Automation: State Machine (FSM) Implementation
--------------------------------------------------
Architecture: Game Loop / FSM
States:
 1. LOGIN:     Login inputs are visible.
 2. LIST:      Desktop list visible, 'Connect' button enabled.
 3. CONNECTING: 'Connect' button disabled OR Native Helper running but Viewer missing.
 4. SESSION:   VDI Viewer process (uSmartView) is running.
 5. UNKNOWN:   Loading or error state.
"""

import os
import sys
import time
import json
import json as py_json
import random
import logging
import urllib.request
import traceback
import subprocess
from enum import Enum

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/var/log/supervisor/automation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("VDI_FSM")

import websocket

# --- CONFIG LOADING ---
def load_config(path='/config/credentials.conf'):
    config = {}
    if not os.path.exists(path):
        return config
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                config[key.strip()] = val.strip().strip('"').strip("'")
    return config

# --- CDP HELPER ---
class CDPSession:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=5)
        self.msg_id = 0
        
    def send(self, method, params=None):
        self.msg_id += 1
        message = { "id": self.msg_id, "method": method, "params": params or {} }
        try:
            self.ws.send(json.dumps(message))
            while True:
                resp = self.ws.recv()
                data = json.loads(resp)
                if data.get("id") == self.msg_id:
                    if "error" in data:
                        return None 
                    return data.get("result")
        except Exception as e:
            return None

    def evaluate(self, expression):
        res = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True
        })
        if not res: return None
        return res.get("result", {}).get("value")

    def reload(self):
        self.send("Page.reload")     

    def is_alive(self):
        """Stealthy heartbeat check using a browser-level command (no JS injection)"""
        try:
            res = self.send("Browser.getVersion")
            if res:
                logger.info(f"Stealth Check Response: {res}")
            return res is not None
        except:
            return False

    def close(self):
        try: self.ws.close()
        except: pass

# --- STATES ---
class State(Enum):
    UNKNOWN = 0       # 初始状态或未定义页面
    LOGIN = 1         # 登录界面 (#/login)
    DESKTOP_LIST = 2  # 云电脑列表主界面 (#/home)
    CONNECTING = 3    # 正在建立桌面连接（加载中）
    IN_SESSION = 4    # 已成功进入桌面会话
    ZOMBIE = 5        # 客户端卡死或无响应状态
    WAIT = 6          # 触发风控或冲突后的冷却等待

# --- MAIN CONTROLLER ---
class VDIStateMachine:
    def __init__(self):
        self.reload_config()
        self.cdp_url = "http://localhost:9222"
        self.session = None
        self.last_keepalive = time.time()
        self.state = State.UNKNOWN
        self.last_state = None
        self.state_start_time = time.time()
        self.last_action_time = 0  # 追踪最后一次尝试操作的时间

    def reload_config(self):
        self.config = load_config()
        self.username = self.config.get('phone', '')
        self.password = self.config.get('password', '')
        self.login_method = self.config.get('login_method', 'password')
        self.connect_index = int(self.config.get('connect_index', 0))
        self.min_int = int(self.config.get('keepalive_min_seconds', 120))
        self.max_int = int(self.config.get('keepalive_max_seconds', 300))
        self.keepalive_method = self.config.get('keepalive_method', 'mouse_move')
        self.conflict_wait = int(self.config.get('conflict_wait_seconds', 300))
        self.keepalive_interval = random.randint(self.min_int, self.max_int)
        self.last_conflict_log = 0

    def get_cdp_session(self):
        """Get or refresh CDP session"""
        if self.session:
            # Check if alive via stealthy heartbeat
            if self.session.is_alive():
                return self.session
            else:
                logger.warning("CDP Session lost. Reconnecting...")
                self.session.close()
                self.session = None

        # Connect new
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json", timeout=3) as f:
                pages = json.load(f)
                logger.info(f"CDP Poll: Found {len(pages)} targets")
                ws_url = next((p['webSocketDebuggerUrl'] for p in pages if p['type'] == 'page'), None)
                if ws_url:
                    self.session = CDPSession(ws_url)
                    return self.session
        except Exception as e:
            logger.error(f"CDP Connect Error: {e}")
            pass
        return None

    def is_process_running(self, name):
        try:
            output = subprocess.check_output(["ps", "aux"]).decode()
            if name not in output:
                return False
            for line in output.split('\n'):
                if name in line:
                    parts = line.split()
                    if len(parts) > 7:
                        stat = parts[7]
                        if 'Z' in stat:
                            return "ZOMBIE"
            return True
        except:
            return False

    def click_at_selector(self, selector, text_hint=None):
        """Find element coordinates and perform a physical click via CDP"""
        s = self.get_cdp_session()
        if not s: return False
        

        target_selector = py_json.dumps(selector)
        target_hint = py_json.dumps(text_hint) if text_hint else "null"
        
        # JS to find element and get its center coordinates with visibility check
        js_find = f"""
            (function() {{
                try {{
                    let el;
                    if ({target_hint} !== null) {{
                        el = Array.from(document.querySelectorAll({target_selector}))
                                  .find(e => e.innerText.includes({target_hint}));
                    }} else {{
                        el = document.querySelector({target_selector});
                    }}
                    if (!el) return null;
                    let rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return null;
                    return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
                }} catch(e) {{ return null; }}
            }})()
        """
        
        pos = s.evaluate(js_find)
        if pos and 'x' in pos and 'y' in pos:
            x, y = pos['x'], pos['y']
            s.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            s.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            return True
        return False

    def paste_at_selector(self, selector, text):
        """Focus element via click, Select All (Ctrl+A), and insert text via CDP (Overwrite mode)"""
        if self.click_at_selector(selector):
            s = self.get_cdp_session()
            time.sleep(1)
            # Select All (Ctrl+A) to ensure we overwrite existing content
            s.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "modifiers": 2, # Control
                "windowsVirtualKeyCode": 65, # A
                "key": "a",
                "code": "KeyA"
            })
            s.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "modifiers": 2,
                "windowsVirtualKeyCode": 65,
                "key": "a",
                "code": "KeyA"
            })
            time.sleep(0.1)
            s.send("Input.insertText", {"text": text})
            return True
        return False

    # --- SENSE (State Detection) 建议应该倒过来看状态---
    def detect_state(self):
        # 1. 最优先判定：桌面会话是否已运行 (IN_SESSION)
        proc_status = self.is_process_running("uSmartView")
        if proc_status == "ZOMBIE":
            return State.ZOMBIE
        if proc_status is True:
            return State.IN_SESSION

        # 2. 获取浏览器会话进行 UI 判定         # 远端桌面不存在 可能当前是登陆或未登陆状态或其它糟糕的状态
        s = self.get_cdp_session()
        if not s:
            return State.UNKNOWN

        try:
            current_url = s.evaluate("window.location.href")

            # 3. 判定是否在列表页 (#/home)
            if "home" in current_url:
                page_text = s.evaluate("document.body.innerText")
                # 检查冲突/挤占状态
                if "其他设备" in page_text or "被挤" in page_text:
                    logger.warning("[SENSE] Conflict Detected -> WAIT")
                    return State.WAIT
                
                # 检查主按钮状态判定是否在连接中
                is_disabled = s.evaluate("document.querySelector('.btn-link') && document.querySelector('.btn-link').disabled")
                if is_disabled:
                    return State.CONNECTING
                return State.DESKTOP_LIST

            # 4. 判定是否在登录页 (#/login)
            elif "login" in current_url:
                # 识别具体登录子视图供日志参考
                login_view = s.evaluate("""
                    (function() {
                        let h6 = document.querySelector('.lf-name h6');
                        if (h6) return h6.innerText;
                        let activeTab = document.querySelector('.lf-tabs .active');
                        if (activeTab) return activeTab.innerText;
                        return 'Unknown Login';
                    })()
                """)
                if login_view:
                    logger.info(f"[SENSE] Login Page Active: {login_view.strip()}")
                return State.LOGIN

            elif "error" in current_url:
                logger.warning("[SENSE] Error Page Detected")
                return State.UNKNOWN

        except Exception as e:
            logger.error(f"[SENSE] Error during detection: {e}")
            
        return State.UNKNOWN

    # --- ACT (State Handlers) ---
    def monitor_state(self, current_state):
        duration = time.time() - self.state_start_time
        s = self.session
        
        if current_state == State.WAIT:
            # User Requested: Wait Configured Time if squeezed
            now = time.time()
            if int(duration) > 0 and (now - self.last_conflict_log >= 60):
                logger.warning(f"[ACT] CONFLICT WAIT: Giving user time... ({duration//60:.0f}/{self.conflict_wait//60:.0f} mins)")
                self.last_conflict_log = now
            
            if duration > self.conflict_wait:
                logger.info("[ACT] WAIT OVER -> Refreshing to check status")
                if s: s.reload()
                self.last_conflict_log = 0
            return

        if current_state == State.LOGIN:
            now = time.time()
            if duration > 10 and (now - self.last_action_time) > 6:
                self.reload_config() # 热更新配置
                logger.info(f"[ACT] LOGIN: Processing {self.login_method} login for {self.username}...")
                self.last_action_time = now
                
                # 1. 确保在正确的登录视图
                view_text = s.evaluate("document.querySelector('.lf-name h6') ? document.querySelector('.lf-name h6').innerText : ''")
                target_text = "子账号登录" if self.login_method == "sub_account" else "账号名密码登录"
                
                if target_text not in view_text:
                    logger.info(f"[ACT] Switching to {target_text} view...")
                    switch_btn_text = "子账号登录" if self.login_method == "sub_account" else "账密登录"
                    if self.click_at_selector(".lf-sub p", text_hint=switch_btn_text):
                        time.sleep(3) # 等待视图切换动画
                
                # 2. 物理模拟填表 (粘贴模式)
                user_ok = self.paste_at_selector("input[placeholder*='账号']", self.username)
                pass_ok = self.paste_at_selector("input[type='password']", self.password)
                # logger.info(f"账号: {self.username}, 密码：{self.password} ok1:{user_ok} , ok2: {pass_ok}")
                logger.info(f"ok1:{user_ok} , ok2: {pass_ok}")
                if user_ok and pass_ok:
                    # 3. 勾选协议
                    is_checked = s.evaluate("document.querySelector('.el-checkbox').classList.contains('is-checked')")
                    if not is_checked:
                        self.click_at_selector(".el-checkbox__inner")
                    
                    # 4. 点击登录
                    time.sleep(1)
                    self.click_at_selector("button.el-button--primary")
                    logger.info("[ACT] Login submitted.")

        elif current_state == State.DESKTOP_LIST:
            now = time.time()
            if duration > 5 and (now - self.last_action_time) > 10:
                logger.info(f"[ACT] LIST: Connecting to desktop index {self.connect_index}...")
                self.last_action_time = now
                
                # 使用物理点击代替 JS 点击
                # 我们通过 JS 找到第 N 个可用按钮的选择器标识
                target_selector = f".h-item-wrap:nth-child({self.connect_index + 1}) .btn-link"
                if self.click_at_selector(target_selector):
                    logger.info("[ACT] Desktop link clicked.")

        elif current_state == State.CONNECTING:
             if int(duration) % 5 == 0:
                 logger.info(f"[ACT] CONNECTING: Waiting for VDI Launch... ({duration:.0f}s)")
             # Watchdog: If connecting > 60s, maybe the app hung?
             if duration > 60:
                 logger.warning("[ACT] CONNECTING timeout -> Reloading UI")
                 if s: s.reload()

        elif current_state == State.IN_SESSION:
            # Keep Alive via CDP (No PyAutoGUI)
            # 必须模拟鼠标移动，否则 VDI 客户端会判定为闲置并断开
            now = time.time()
            if now - self.last_keepalive > self.keepalive_interval:
                try:
                    s = self.get_cdp_session()
                    if s:
                        # 产生一个随机的“拟人”坐标
                        # 在 200-600 像素的中间安全区域抖动，避免意外点到边角的退出或关闭按钮
                        rx, ry = random.randint(200, 600), random.randint(200, 600)
                        s.send("Input.dispatchMouseEvent", {
                            "type": "mouseMoved",
                            "x": rx,
                            "y": ry
                        })
                        logger.info(f"[ACT] IN_SESSION: Mouse Jiggle to ({rx}, {ry}) to keep alive.")
                except Exception as e:
                    logger.error(f"Heartbeat Jiggle Failed: {e}")
                
                self.last_keepalive = now
                self.keepalive_interval = random.randint(self.min_int, self.max_int)

        elif current_state == State.UNKNOWN:
             if duration > 30:
                 logger.error("[ACT] UNKNOWN STUCK (>30s) -> FORCE RELOAD")
                 if s: s.reload()

        elif current_state == State.ZOMBIE:
            logger.error("[ACT] ZOMBIE PROCESS -> KILLING")
            subprocess.call(["pkill", "-9", "-f", "uSmartView"])

    # --- LOOP ---
    def run(self):
        logger.info(">>> VDI FSM Bot Started (Router-Aware)")
        while True:
            try:
                # 1. Sense
                new_state = self.detect_state()
                
                # 2. State Transition
                if new_state != self.state:
                    logger.info(f"TRANSITION: {self.state.name} -> {new_state.name}")
                    self.state = new_state
                    self.state_start_time = time.time()
                    self.last_action_time = 0 # 状态切换时重置操作计时器
                
                # 3. Act
                self.monitor_state(new_state)
                
                # 4. Tick
                time.sleep(2)
                    
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Loop Crash: {e}")
                time.sleep(5)
                self.session = None

if __name__ == "__main__":
    time.sleep(5)
    bot = VDIStateMachine()
    bot.run()
