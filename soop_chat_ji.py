import asyncio
import websockets
import urllib.request
import urllib.parse
import json
import ssl
import re
from datetime import datetime, timedelta
from typing import Optional, List
from collections import deque
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import uvicorn

# --- 설정 ---
TARGET_BJ_ID = "tjrdbs999"
TARGET_BJ_NAME = "지피티"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 제어 문자 및 상수
F = "\x0c"
ESC = "\x1b\t"

class ServiceType:
    PING           = "0000"
    LOGIN          = "0001"
    JOIN           = "0002"
    CHATTING       = "0005"
    BALLOON        = "0018"

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
    
    # 밈별 데이터
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

class MemeScanner:
    def __init__(self, key, pattern, name_kr):
        self.key = key
        self.pattern = pattern
        self.name_kr = name_kr
        
        self.wave_count = 0
        self.total_count = 0
        
        # Streak 상태
        self.streak_start = None
        self.streak_last = None
        self.streak_count = 0
        self.streak_confirmed = False

    def reset(self):
        """세션 시작 시 초기화"""
        self.wave_count = 0
        self.total_count = 0
        self.reset_streak()
    
    def reset_streak(self):
        self.streak_start = None
        self.streak_last = None
        self.streak_count = 0
        self.streak_confirmed = False

    def process(self, msg, now):
        """메시지 처리 및 웨이브 감지"""
        if re.search(self.pattern, msg):
            self.total_count += 1
            
            # --- [지속성 감지 로직] ---
            # 1. 10초 이상 끊기면 리셋
            if self.streak_last and (now - self.streak_last).total_seconds() > 10:
                self.reset_streak()
            
            # 2. 시작점 설정
            if self.streak_start is None:
                self.streak_start = now
                self.streak_count = 0
                self.streak_confirmed = False
            
            self.streak_count += 1
            self.streak_last = now
            
            # 3. 판단 (10초 지속, 20개 메시지)
            dt_duration = (now - self.streak_start).total_seconds()
            
            if dt_duration >= 10 and self.streak_count >= 20:
                if not self.streak_confirmed:
                    self.wave_count += 1
                    self.streak_confirmed = True
                    print(f"🌊 [WAVE] {self.name_kr} 10초 지속 확정! (시즌 {self.wave_count}회)")
            
            return True
        return False

class AutoMonitorBot:
    def __init__(self):
        self.is_live = False
        self.current_bno = None       # 현재 방송 번호
        self.broadcast_title = "방송 준비 중"
        self.broadcast_start_time = None
        
        # 밈 스캐너 초기화
        self.scanners = {
            "ji_chang": MemeScanner("ji_chang", r"지[ㅡ\s~-]*창", "지창"),
            "sesin": MemeScanner("sesin", r"세[ㅡ\s~-]*신", "세신"),
            "jjajang": MemeScanner("jjajang", r"짜[ㅡ\s~-]*장[ㅡ\s~-]*면", "짜장면"),
            "djrg": MemeScanner("djrg", r"ㄷ[ㅡ\s~-]*ㅈ[ㅡ\s~-]*ㄹ[ㅡ\s~-]*ㄱ", "ㄷㅈㄹㄱ"),
        }
        
        self.last_detected_at = None
        
        # 이슈 감지
        
        # 지창 지속 감지(Streak) 변수
        self.streak_start_time = None
        self.streak_last_time = None
        self.streak_msg_count = 0
        self.streak_confirmed = False
        
        # 이슈 감지
        self.window_seconds = 30
        self.threshold_count = 10
        self.timestamps = deque()
        self.hot_moments = []
        self.last_hot_time = None
        self.history = []         # 지난 방송 기록 (성적표)
        
        self.queue = asyncio.Queue()
        self.ws_task = None
        self.monitor_task = None

    async def run_forever(self):
        """지능형 자동 감지 루프"""
        print(f"🤖 [{TARGET_BJ_NAME}] 스마트 감지 봇 가동 (ID: {TARGET_BJ_ID})")
        
        # 리방(방송 재시작) 감지를 위한 변수
        self.last_stream_end_time = None
        
        while True:
            try:
                # 1. 현재 방송 상태 확인
                broad_info = await self.check_live_status()
                now = datetime.now()
                
                if broad_info:
                    # [방송 중]
                    if not self.is_live or self.current_bno != broad_info['broad_no']:
                        print(f"\n📺 방송 시작 감지! ({broad_info['broad_title']})")
                        await self.start_session(broad_info)
                    
                    # 방송 중일 때는 API 호출을 최대한 아끼고 WebSocket 유지에 집중
                    # 단, 1분마다 방송 정보(제목 등) 업데이트를 위해 체크
                    sleep_time = 60 

                else:
                    # [방송 OFF]
                    if self.is_live:
                        print(f"\n💤 방송 종료 감지. ({datetime.now().strftime('%H:%M:%S')})")
                        await self.stop_session()
                        self.last_stream_end_time = datetime.now()

                    # --- 스마트 스케줄링 (API 호출 최소화 전략) ---
                    # 1. 리방 의심 구간: 방송 종료 후 9분간은 3분마다 체크 (약 3회)
                    if self.last_stream_end_time and (now - self.last_stream_end_time).total_seconds() < 540:
                        sleep_time = 180
                    
                    # 2. 피크 타임 (오후 4시 ~ 6시): 3분마다 체크 (집중 감시 구간)
                    elif 16 <= now.hour < 18:
                        sleep_time = 180
                        
                    # 3. 그 외 (18시 이후 포함): 10분마다 체크 (절전 모드)
                    else:
                        sleep_time = 600
                
                await asyncio.sleep(sleep_time)
                
            except Exception as e:
                print(f"⚠️ 감시 루프 에러: {e}")
                await asyncio.sleep(60)

    async def check_live_status(self):
        """아프리카TV Station API로 방송 여부 확인"""
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
        """새로운 방송 세션 시작"""
        await self.stop_session() 
        self.is_live = True
        self.current_bno = broad_info['broad_no']
        self.broadcast_title = broad_info['broad_title']
        self.broadcast_start_time = broad_info['start_time']
        
        # 통계 초기화
        for scanner in self.scanners.values():
            scanner.reset()
        self.last_detected_at = None
        
        self.timestamps.clear()
        self.hot_moments.clear()
        self.last_hot_time = None
        
        chat_info = await self.get_chat_connection_info(self.current_bno)
        if chat_info:
            self.ws_task = asyncio.create_task(self.connect_websocket(chat_info))
    
    async def stop_session(self):
        """세션 종료 및 정리 (성적표 저장)"""
        if self.is_live:
            # 방송 기록 저장 (방송 시작 날짜 기준)
            try:
                # 아프리카TV 시간 포맷 (예: 2023-10-10 18:00:00)
                date_str = self.broadcast_start_time.split(' ')[0]
            except:
                date_str = datetime.now().strftime('%Y-%m-%d')

            self.history.insert(0, {
                "date": date_str,
                "title": self.broadcast_title,
                "total_ji_chang": self.scanners["ji_chang"].wave_count,
                "total_sesin": self.scanners["sesin"].wave_count,
                "total_jjajang": self.scanners["jjajang"].wave_count,
                "total_djrg": self.scanners["djrg"].wave_count
            })
            # 히스토리 50개 유지
            if len(self.history) > 50:
                self.history.pop()
            
            print(f"✅ 방송 종료 기록 저장: {date_str} | 지창W: {self.scanners['ji_chang'].wave_count}")

        self.is_live = False
        self.current_bno = None  # 확실하게 초기화
        self.broadcast_title = "방송 준비 중"
        
        if self.ws_task:
            self.ws_task.cancel()
            self.ws_task = None

    async def get_chat_connection_info(self, bno):
        """채팅 서버 접속에 필요한 상세 정보 로드"""
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
        """웹소켓 연결 및 패킷 처리"""
        uri = f"wss://{info['DOMAIN']}:{info['CHPT']}/Websocket/{info['BID']}"
        print(f"🔗 채팅 서버 연결 시도: {uri}")
        
        try:
            async with websockets.connect(uri, subprotocols=['chat'], ssl=ssl._create_unverified_context(), ping_interval=None) as ws:
                # 로그인 & 조인
                await ws.send(self.create_packet(ServiceType.LOGIN, f"{F*3}16{F}"))
                await ws.recv() # Login Response
                
                join_body = f"{F}{info['CHATNO']}{F}{info['FTK']}{F}0{F}{F}"
                await ws.send(self.create_packet(ServiceType.JOIN, join_body))
                
                print("✅ 채팅 서버 연결 성공! 지창 감시 중...")

                ping_task = asyncio.create_task(self.send_ping(ws))
                
                try:
                    async for raw_data in ws:
                        await self.handle_packet(raw_data)
                except Exception as e:
                    print(f"연결 끊김: {e}")
                finally:
                    ping_task.cancel()
                    
        except Exception:
            # 연결 실패 시 잠시 대기 (AutoLoop가 알아서 다시 시도하거나 처리함)
            pass

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
            cmd = parts[0][2:6]

            if cmd == ServiceType.CHATTING:
                msg, nickname = parts[1], parts[6]
                if msg in ["-1", "1"] or "fw=" in msg: return
                
                # 모든 스캐너 체크
                detected = False
                now = datetime.now()
                for key, scanner in self.scanners.items():
                    if scanner.process(msg, now):
                        detected = True
                        self.last_detected_at = now
                        # 로그 출력 (선택)
                        # print(f"🔥 {scanner.name_kr} ({scanner.total_count}) | {nickname}: {msg}")

                if detected:
                    # 핫타임 로직 (통합 이슈 감지용)
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
                                "description": f"30초간 {density}회 밈 폭주!"
                            })
                            # 메모리 보호: 최근 100개 이슈만 유지
                            if len(self.hot_moments) > 100:
                                self.hot_moments.pop()
                                
                            self.last_hot_time = now
                            print(f"\n🔥🔥 [이슈] {now.strftime('%H:%M:%S')} - 30초 {density}회 반응!")
        except Exception: pass

# --- FastAPI App ---
app = FastAPI(title="SOOP 지피티 지창 봇")
bot = AutoMonitorBot()

@app.on_event("startup")
async def startup_event():
    # 앱 시작 시 봇을 백그라운드 태스크로 실행
    asyncio.create_task(bot.run_forever())

@app.get("/")
async def root():
    return {"message": f"Soop Ji-Chang Bot for {TARGET_BJ_NAME} is Running."}

@app.get("/health")
async def health_check():
    """서버 상태 확인용 (Uptime Check)"""
    return {"status": "ok"}

@app.get("/stats", response_model=StatsResponse)
async def get_stats():
    """현재 방송의 지창 통계"""
    return StatsResponse(
        status="LIVE" if bot.is_live else "WAITING",
        broadcast_title=bot.broadcast_title,
        started_at=bot.broadcast_start_time,
        
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
    uvicorn.run("soop_chat_ji:app", host="0.0.0.0", port=8000, reload=True)
