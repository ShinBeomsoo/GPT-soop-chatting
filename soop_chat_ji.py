import asyncio
import websockets
import urllib.request
import urllib.parse
import json
from functools import lru_cache
import ssl
import re
import sys

# 제어 문자 및 상수
F = "\x0c"
ESC = "\x1b\t"

class Flag1:
    BJ             = 4
    FANCLUB        = 32         # 1 << 5
    MANAGER        = 256        # 1 << 8
    TOPFAN         = 32768      # 1 << 15
    FOLLOWER       = 268435456  # 1 << 28 (구독자)

class ServiceType:
    PING           = "0000"
    LOGIN          = "0001"
    JOIN           = "0002"
    CHATTING       = "0005"     # SVC_CHATMESG
    BALLOON        = "0018"     # SVC_SENDBALLOON
    NOTICE         = "0104"     # SVC_NOTICE

class SoopChatClient:
    def __init__(self, mode="ALL"):
        self.url = ""
        self.info = None
        self.mode = mode
        self.is_running = True
        self.ji_chang_count = 0  # 지창 카운트
        self.queue = asyncio.Queue()

    def validate_url(self, url):
        """URL 유효성 검사"""
        pattern = r"https?://play\.(sooplive\.co\.kr|afreecatv\.com)/[a-zA-Z0-9]+/\d+"
        return bool(re.match(pattern, url))

    async def get_broadcast_info(self):
        """방송 정보 로드"""
        try:
            parts = self.url.rstrip('/').split('/')
            bid, bno = parts[-2], parts[-1]
            api_url = 'https://live.afreecatv.com/afreeca/player_live_api.php'
            data = urllib.parse.urlencode({
                'bid': bid, 'bno': bno, 'type': 'live', 'player_type': 'html5'
            }).encode()
            
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, lambda: json.loads(
                urllib.request.urlopen(urllib.request.Request(api_url, data=data)).read()
            ))
            
            channel = res.get("CHANNEL")
            if not channel: return False

            self.info = {
                "DOMAIN": channel["CHDOMAIN"].lower(),
                "CHATNO": channel["CHATNO"],
                "FTK": channel["FTK"],
                "CHPT": str(int(channel["CHPT"]) + 1),
                "BID": channel["BJID"],
                "TITLE": channel.get("TITLE", "Live Broadcasting")
            }
            return True
        except:
            return False

    def create_packet(self, service_type, body):
        """패킷 헤더 생성"""
        body_bytes = body.encode('utf-8')
        header = f"{service_type}{len(body_bytes):06}00"
        return ESC.encode('utf-8') + header.encode('utf-8') + body_bytes

    @lru_cache(maxsize=1024)
    def parse_flags(self, flag_part):
        """유저 권한 파싱"""
        try:
            f1 = int(flag_part.split('|')[0])
            status = []
            if f1 & Flag1.BJ: status.append("BJ")
            if f1 & Flag1.MANAGER: status.append("매니저")
            if f1 & Flag1.TOPFAN: status.append("열혈")
            if f1 & Flag1.FOLLOWER: status.append("구독")
            if f1 & Flag1.FANCLUB: status.append("팬")
            return f"[{'/'.join(status)}]" if status else "[일반]"
        except: return "[?]"

    async def send_ping(self, ws):
        """수동 핑"""
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(self.create_packet(ServiceType.PING, F))
        except asyncio.CancelledError: pass

    async def handle_packet(self, raw_data):
        """패킷 파싱 및 '지창' 카운트"""
        try:
            decoded = raw_data.decode('utf-8', errors='ignore')
            parts = decoded.split(F)
            cmd = parts[0][2:6]

            # 1. 채팅 (0005)
            if cmd == ServiceType.CHATTING:
                msg, nickname, flags = parts[1], parts[6], parts[7]
                if msg in ["-1", "1"] or "fw=" in msg: return
                
                # '지창' 감지 로직 (정규식: 지ㅡ창, 지~~창, 지 창 포함)
                if re.search(r"지[ㅡ\s~-]*창", msg):
                    self.ji_chang_count += 1
                    # 지창 외칠 때만 강조 출력 (옵션)
                    print(f"🔥 지창 감지! ({self.ji_chang_count}회) | {nickname}: {msg}")
                else:
                    # 일반 채팅은 그냥 출력 (원하면 주석 처리하여 조용히 카운트만 가능)
                    badge = self.parse_flags(flags)
                    print(f"{badge} {nickname}: {msg}")

            # 2. 별풍선 후원 (무시함)
            elif cmd == ServiceType.BALLOON:
                pass

        except Exception: pass

    async def run(self):
        """메인 실행"""
        while True:
            url_input = input("SOOP 방송 주소를 입력하세요: ").strip()
            if self.validate_url(url_input):
                self.url = url_input
                break
            print("유효하지 않은 주소입니다.")

        print(f"모드: '지창' 카운터 | 감시 시작...")

        while self.is_running:
            if not await self.get_broadcast_info():
                print("방송 정보를 가져오지 못했습니다. 10초 후 재시도...")
                await asyncio.sleep(10)
                continue

            workers = [asyncio.create_task(self.packet_worker()) for _ in range(3)]
            uri = f"wss://{self.info['DOMAIN']}:{self.info['CHPT']}/Websocket/{self.info['BID']}"
            
            try:
                async with websockets.connect(uri, subprotocols=['chat'], ssl=ssl._create_unverified_context(), ping_interval=None) as ws:
                    await ws.send(self.create_packet(ServiceType.LOGIN, f"{F*3}16{F}"))
                    await ws.recv()
                    
                    join_body = f"{F}{self.info['CHATNO']}{F}{self.info['FTK']}{F}0{F}{F}"
                    await ws.send(self.create_packet(ServiceType.JOIN, join_body))
                    
                    print(f"연결 성공: {self.info['TITLE']} (종료: Ctrl+C)")
                    ping_task = asyncio.create_task(self.send_ping(ws))

                    async for raw_data in ws:
                        await self.queue.put(raw_data)
                    
                    ping_task.cancel()
            except Exception as e:
                print(f"연결 종료 ({e})...")
                await asyncio.sleep(5)
            finally:
                for w in workers: w.cancel()

    async def packet_worker(self):
        while True:
            raw_data = await self.queue.get()
            try:
                await self.handle_packet(raw_data)
            except: pass
            finally:
                self.queue.task_done()

if __name__ == "__main__":
    client = SoopChatClient()
    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        print(f"\n✋ 프로그램이 종료되었습니다.")
        print(f"📊 오늘 '지창'은 총 {client.ji_chang_count}번 외쳐졌습니다!")
