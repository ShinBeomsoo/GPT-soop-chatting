import asyncio
import websockets
import urllib.request
import urllib.parse
import json
import ssl
import re
import uvicorn
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from collections import deque
from fastapi import FastAPI
from pydantic import BaseModel

# ==============================================================================
# 1. Configuration & Constants
# ==============================================================================
TARGET_BJ_ID = "tjrdbs999"
TARGET_BJ_NAME = "지피티"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 밈(Meme) 감지 설정 정의
MEME_CONFIG = [
    {"key": "ji_chang", "name": "지창",  "pattern": r"지[ㅡ\s~-]*창"},
    {"key": "sesin",    "name": "세신",  "pattern": r"세[ㅡ\s~-]*신"},
    {"key": "jjajang",  "name": "짜장면", "pattern": r"짜[ㅡ\s~-]*장[ㅡ\s~-]*면"},
    {"key": "djrg",     "name": "ㄷㅈㄹㄱ","pattern": r"ㄷ[ㅡ\s~-]*ㅈ[ㅡ\s~-]*ㄹ[ㅡ\s~-]*ㄱ"},
]

# 아프리카TV 채팅 프로토콜 상수
F = "\x0c"
ESC = "\x1b\t"

class ServiceType:
    PING     = "0000"
    LOGIN    = "0001"
    JOIN     = "0002"
    CHATTING = "0005"

# ==============================================================================
# 2. Pydantic Models (API Response)
# ==============================================================================
class HotMoment(BaseModel):
    time: str
    count: int
    description: str

class BroadcastHistory(BaseModel):
    date: str
    title: str
    total_ji_chang: int
    total_sesin: int = 0
    total_jjajang: int = 0
    total_djrg: int = 0

class StatsResponse(BaseModel):
    status: str            # LIVE / WAITING
    broadcast_title: str
    started_at: Optional[str] = None
    
    # [Meme Stats]
    ji_chang_wave_count: int
    total_ji_chang_chat_count: int
    
    sesin_wave_count: int
    total_sesin_chat_count: int
    
    jjajang_wave_count: int
    total_jjajang_chat_count: int
    
    djrg_wave_count: int
    total_djrg_chat_count: int
    
    last_detected_at: Optional[datetime] = None
    hot_moments: List[HotMoment] = []
    history: List[BroadcastHistory] = []

# ==============================================================================
# 3. Core Logic Classes
# ==============================================================================
class MemeScanner:
    """개별 밈의 패턴 매칭 및 Wave(물타기) 감지 로직"""
    def __init__(self, key: str, pattern: str, name_kr: str):
        self.key = key
        self.pattern = pattern
        self.name_kr = name_kr
        
        # 통계 데이터
        self.wave_count = 0
        self.total_count = 0
        
        # Wave(Streak) 감지 상태
        self.streak_start = None
        self.streak_last = None
        self.streak_count = 0
        self.streak_confirmed = False

    def reset(self):
        """세션 시작 시 상태 초기화"""
        self.wave_count = 0
        self.total_count = 0
        self.reset_streak()
    
    def reset_streak(self):
        """연속 흐름 초기화"""
        self.streak_start = None
        self.streak_last = None
        self.streak_count = 0
        self.streak_confirmed = False

    def process(self, msg: str, now: datetime) -> bool:
        """메시지를 분석하여 밈 카운트 및 Wave 감지 수행"""
        if not re.search(self.pattern, msg):
            return False

        self.total_count += 1
        
        # --- [Wave 감지 로직] ---
        # 1. 흐름 끊김 체크 (10초 이상 공백)
        if self.streak_last and (now - self.streak_last).total_seconds() > 10:
            self.reset_streak()
        
        # 2. 새로운 흐름 시작
        if self.streak_start is None:
            self.streak_start = now
            self.streak_count = 0
            self.streak_confirmed = False
        
        self.streak_count += 1
        self.streak_last = now
        
        # 3. Wave 판정 (10초 지속 AND 20개 이상)
        dt_duration = (now - self.streak_start).total_seconds()
        
        if dt_duration >= 10 and self.streak_count >= 20:
            if not self.streak_confirmed:
                self.wave_count += 1
                self.streak_confirmed = True
                print(f"🌊 [WAVE] {self.name_kr} 10초 지속 확정! (시즌 {self.wave_count}회)")
        
        return True

class AutoMonitorBot:
    """방송 감지 및 채팅 모니터링 메인 컨트롤러"""
    def __init__(self):
        self.is_live = False
        self.current_bno = None
        self.broadcast_title = "방송 준비 중"
        self.broadcast_start_time = None
        
        # 스캐너 초기화 (설정 기반)
        self.scanners = {
            cfg["key"]: MemeScanner(cfg["key"], cfg["pattern"], cfg["name"])
            for cfg in MEME_CONFIG
        }
        
        self.last_detected_at = None
        
        # 통합 이슈 감지 (채팅량 급증)
        self.window_seconds = 30
        self.threshold_count = 10
        self.timestamps = deque()
        self.hot_moments = []
        self.last_hot_time = None
        
        self.history = self.load_history()  # 파일에서 기록 로드
        self.ws_task = None

    def load_history(self) -> List[dict]:
        """history.json에서 기록 불러오기"""
        try:
            with open("history.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    async def run_forever(self):
        """메인 실행 루프"""
        print(f"🤖 [{TARGET_BJ_NAME}] 스마트 감지 봇 가동 (ID: {TARGET_BJ_ID})")
        self.last_stream_end_time = None
        
        while True:
            try:
                broad_info = await self.check_live_status()
                now = datetime.now()
                
                if broad_info:
                    # [방송 중]
                    if not self.is_live or self.current_bno != broad_info['broad_no']:
                        print(f"\n📺 방송 시작 감지! ({broad_info['broad_title']})")
                        await self.start_session(broad_info)
                    
                    sleep_time = 60 # 방송 중에는 1분 간격 체크
                else:
                    # [방송 OFF]
                    if self.is_live:
                        print(f"\n💤 방송 종료 감지. ({datetime.now().strftime('%H:%M:%S')})")
                        await self.stop_session()
                        self.last_stream_end_time = datetime.now()

                    # 스마트 폴링 주기 설정
                    sleep_time = 600
                    if self.last_stream_end_time and (now - self.last_stream_end_time).total_seconds() < 540:
                        sleep_time = 180 # 리방 의심 (3분)
                    elif 16 <= now.hour < 18:
                        sleep_time = 180 # 피크 타임 (3분)
                
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                print(f"⚠️ 루프 에러: {e}")
                await asyncio.sleep(60)

    async def check_live_status(self):
        """Station API 방송 확인"""
        try:
            url = f"https://bjapi.afreecatv.com/api/{TARGET_BJ_ID}/station"
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: json.loads(
                urllib.request.urlopen(req).read()
            ))
            
            broad = res.get("broad")
            if broad:
                return {
                    "broad_no": broad["broad_no"],
                    "broad_title": broad["broad_title"],
                    "start_time": broad.get("broad_start")
                }
        except Exception:
            pass
        return None

    async def start_session(self, broad_info):
        """세션 시작"""
        await self.stop_session() 
        self.is_live = True
        self.current_bno = broad_info['broad_no']
        self.broadcast_title = broad_info['broad_title']
        self.broadcast_start_time = broad_info['start_time']
        
        # 상태 초기화
        for scanner in self.scanners.values():
            scanner.reset()
        self.last_detected_at = None
        self.timestamps.clear()
        self.hot_moments.clear()
        
        # 채팅 서버 연결
        chat_info = await self.get_chat_connection_info(self.current_bno)
        if chat_info:
            self.ws_task = asyncio.create_task(self.connect_websocket(chat_info))
    
    async def stop_session(self):
        """세션 종료 및 정리"""
        if self.is_live:
            self.save_history()
            
        self.is_live = False
        self.current_bno = None
        self.broadcast_title = "방송 준비 중"
        
        if self.ws_task:
            self.ws_task.cancel()
            self.ws_task = None

    def save_history(self):
        """방송 기록 저장 (파일 영구 저장)"""
        try:
            date_str = self.broadcast_start_time.split(' ')[0]
        except:
            date_str = datetime.now().strftime('%Y-%m-%d')

        record = {
            "date": date_str,
            "title": self.broadcast_title,
            "total_ji_chang": self.scanners["ji_chang"].wave_count,
            "total_sesin": self.scanners["sesin"].wave_count,
            "total_jjajang": self.scanners["jjajang"].wave_count,
            "total_djrg": self.scanners["djrg"].wave_count
        }

        self.history.insert(0, record)
        
        if len(self.history) > 50:
            self.history.pop()
            
        # 파일 저장
        try:
            with open("history.json", "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 히스토리 저장 실패: {e}")
        
        print(f"✅ 리포트 저장 완료: {date_str} (J:{record['total_ji_chang']} S:{record['total_sesin']} Jj:{record['total_jjajang']} D:{record['total_djrg']})")

    async def get_chat_connection_info(self, bno):
        """채팅 접속 정보 로드"""
        try:
            api_url = 'https://live.afreecatv.com/afreeca/player_live_api.php'
            data = urllib.parse.urlencode({
                'bid': TARGET_BJ_ID, 'bno': bno, 'type': 'live', 'player_type': 'html5'
            }).encode()
            
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: json.loads(
                urllib.request.urlopen(urllib.request.Request(api_url, data=data)).read()
            ))
            
            channel = res.get("CHANNEL")
            if not channel: return None

            return {
                "DOMAIN": channel["CHDOMAIN"].lower(),
                "CHATNO": channel["CHATNO"],
                "FTK": channel["FTK"],
                "CHPT": str(int(channel["CHPT"]) + 1),
                "BID": channel["BJID"]
            }
        except:
            return None

    def create_packet(self, service_type, body):
        body_bytes = body.encode('utf-8')
        header = f"{service_type}{len(body_bytes):06}00"
        return ESC.encode('utf-8') + header.encode('utf-8') + body_bytes

    async def connect_websocket(self, info):
        """WebSocket 연결 및 루프"""
        uri = f"wss://{info['DOMAIN']}:{info['CHPT']}/Websocket/{info['BID']}"
        print(f"🔗 채팅 서버 연결: {uri}")
        
        try:
            # SSL Context (Self-signed certs acceptable)
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            async with websockets.connect(uri, subprotocols=['chat'], ssl=ssl_ctx, ping_interval=None) as ws:
                # Login & Join Handshake
                await ws.send(self.create_packet(ServiceType.LOGIN, f"{F*3}16{F}"))
                await ws.recv() 
                
                join_body = f"{F}{info['CHATNO']}{F}{info['FTK']}{F}0{F}{F}"
                await ws.send(self.create_packet(ServiceType.JOIN, join_body))
                
                print("✅ 채팅 감시 시작...")
                ping_task = asyncio.create_task(self.send_ping(ws))
                
                try:
                    async for raw_data in ws:
                        await self.handle_packet(raw_data)
                except Exception as e:
                    print(f"❌ WS 연결 끊김: {e}")
                finally:
                    ping_task.cancel()
        except Exception as e:
            print(f"⚠️ WS 접속 실패: {e}")

    async def send_ping(self, ws):
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(self.create_packet(ServiceType.PING, F))
        except asyncio.CancelledError: pass

    async def handle_packet(self, raw_data):
        try:
            decoded = raw_data.decode('utf-8', errors='ignore')
            parts = decoded.split(F)
            if len(parts) < 7: return
            
            cmd = parts[0][2:6]

            if cmd == ServiceType.CHATTING:
                msg, nickname = parts[1], parts[6]
                if msg in ["-1", "1"] or "fw=" in msg: return
                
                # Active Scanners Check
                detected = False
                now = datetime.now()
                for scanner in self.scanners.values():
                    if scanner.process(msg, now):
                        detected = True
                        self.last_detected_at = now

                if detected:
                    self._check_hot_moment(now)

        except Exception: pass
    
    def _check_hot_moment(self, now):
        """통합 이슈(트래픽 급증) 감지 로직"""
        self.timestamps.append(now)
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        
        density = len(self.timestamps)
        if density >= self.threshold_count:
            if not self.last_hot_time or (now - self.last_hot_time).total_seconds() > 60:
                self.hot_moments.insert(0, {
                    "time": now.strftime('%Y-%m-%d %H:%M:%S'),
                    "count": density,
                    "description": f"30초간 {density}회 밈 반응 폭주!"
                })
                if len(self.hot_moments) > 100:
                    self.hot_moments.pop()
                    
                self.last_hot_time = now
                print(f"🔥 [HOT] {now.strftime('%H:%M:%S')} - 30초간 {density}회 감지됨!")

# ==============================================================================
# 4. FastAPI Setup
# ==============================================================================
app = FastAPI(title="SOOP 지피티 지창 봇")
bot = AutoMonitorBot()

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(bot.run_forever())

@app.get("/")
async def root():
    return {"message": f"Soop Ji-Chang Bot for {TARGET_BJ_NAME} is Running."}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    return StatsResponse(
        status="LIVE" if bot.is_live else "WAITING",
        broadcast_title=bot.broadcast_title,
        started_at=bot.broadcast_start_time,
        
        # Mapped from scanners
        ji_chang_wave_count=bot.scanners["ji_chang"].wave_count,
        total_ji_chang_chat_count=bot.scanners["ji_chang"].total_count,
        
        sesin_wave_count=bot.scanners["sesin"].wave_count,
        total_sesin_chat_count=bot.scanners["sesin"].total_count,
        
        jjajang_wave_count=bot.scanners["jjajang"].wave_count,
        total_jjajang_chat_count=bot.scanners["jjajang"].total_count,
        
        djrg_wave_count=bot.scanners["djrg"].wave_count,
        total_djrg_chat_count=bot.scanners["djrg"].total_count,
        
        last_detected_at=bot.last_detected_at,
        hot_moments=bot.hot_moments,
        history=bot.history
    )

if __name__ == "__main__":
    uvicorn.run("soop_chat_ji:app", host="0.0.0.0", port=8080, reload=True)
