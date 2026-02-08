"""
SOOP 지피티 채팅 모니터링 봇
============================

이 모듈은 아프리카TV(SOOP) 방송의 채팅을 실시간으로 모니터링하고,
특정 밈(Meme) 패턴을 감지하여 통계를 수집합니다.

주요 기능:
- 방송 상태 자동 감지 (온/오프라인)
- 밈 패턴 실시간 감지 (지창, 세신, 짜장면, ㄷㅈㄹㄱ, ㅆㄷㄴ)
- Wave(물타기) 현상 추적
- Hot Moment(급증 구간) 감지
- REST API를 통한 통계 조회
"""

from __future__ import annotations

import asyncio
import json
import re
import ssl
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

import uvicorn
import websockets
from fastapi import FastAPI
from pydantic import BaseModel

# ==============================================================================
# 설정 (Configuration)
# ==============================================================================

@dataclass(frozen=True)
class BroadcasterConfig:
    """방송자(BJ) 설정을 담는 불변 데이터 클래스"""
    id: str
    name: str


@dataclass(frozen=True)
class MemePattern:
    """밈 패턴 정의를 위한 불변 데이터 클래스"""
    key: str
    display_name: str
    regex_pattern: str


# 앱 설정 상수
TARGET_BROADCASTER = BroadcasterConfig(id="cnsgkcnehd74", name="조경훈")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 밈 패턴 정의
MEME_PATTERNS = (
    MemePattern("ji_chang", "지창", r"지[ㅡ\s~-]*창"),
    MemePattern("sesin", "세신", r"세[ㅡ\s~-]*신"),
    MemePattern("jjajang", "짜장면", r"짜[ㅡ\s~-]*장[ㅡ\s~-]*면"),
    MemePattern("djrg", "ㄷㅈㄹㄱ", r"ㄷ[ㅡ\s~-]*ㅈ[ㅡ\s~-]*ㄹ[ㅡ\s~-]*ㄱ"),
    # ㅆㄷㄴ / 쌋다나 / 쌌다나
    MemePattern(
        "sdn",
        "ㅆㄷㄴ",
        r"(?:ㅆ[ㅡ\s~-]*ㄷ[ㅡ\s~-]*ㄴ|쌋[ㅡ\s~-]*다[ㅡ\s~-]*나|쌌[ㅡ\s~-]*다[ㅡ\s~-]*나)"
    ),
)


def _extract_date_from_start_time(start_time: Optional[str]) -> str:
    """시작 시간 문자열에서 날짜(YYYY-MM-DD)를 추출합니다. 없으면 오늘 날짜 반환."""
    if start_time:
        try:
            return start_time.split(" ")[0]
        except (AttributeError, IndexError):
            pass
    return datetime.now().strftime("%Y-%m-%d")


# ==============================================================================
# 아프리카TV 채팅 프로토콜 상수
# ==============================================================================

class ChatProtocol:
    """아프리카TV 채팅 프로토콜 상수 정의"""
    FIELD_SEPARATOR = "\x0c"
    ESCAPE_SEQUENCE = "\x1b\t"
    
    class ServiceType:
        """서비스 타입 코드"""
        PING = "0000"
        LOGIN = "0001"
        JOIN = "0002"
        CHATTING = "0005"


# ==============================================================================
# API 응답 모델 (Pydantic)
# ==============================================================================

class HotMomentResponse(BaseModel):
    """Hot Moment(급증 구간) 응답 모델"""
    time: str
    count: int
    description: str


class BroadcastHistoryResponse(BaseModel):
    """방송 기록 응답 모델"""
    date: str
    title: str
    total_ji_chang: int
    total_sesin: int = 0
    total_jjajang: int = 0
    total_djrg: int = 0
    total_sdn: int = 0


class StatsResponse(BaseModel):
    """통계 API 응답 모델"""
    status: str  # LIVE 또는 WAITING
    broadcast_title: str
    started_at: Optional[str] = None
    
    # 밈별 통계
    ji_chang_wave_count: int
    total_ji_chang_chat_count: int
    
    sesin_wave_count: int
    total_sesin_chat_count: int
    
    jjajang_wave_count: int
    total_jjajang_chat_count: int
    
    djrg_wave_count: int
    total_djrg_chat_count: int
    
    sdn_wave_count: int
    total_sdn_chat_count: int
    
    last_detected_at: Optional[datetime] = None
    hot_moments: list[HotMomentResponse] = []
    history: list[BroadcastHistoryResponse] = []


class HotMomentFileResponse(BaseModel):
    """Hot Moment 파일 응답 모델"""
    time: str
    count: int
    description: str
    detected_memes: list[str] = []


class SessionResponse(BaseModel):
    """방송 세션 응답 모델"""
    broadcast_title: str
    saved_at: str
    hot_moments: list[HotMomentFileResponse]
    # 세션별 밈 총 감지 횟수
    total_ji_chang: int = 0
    total_sesin: int = 0
    total_jjajang: int = 0
    total_djrg: int = 0
    total_sdn: int = 0


class DailyHistoryResponse(BaseModel):
    """날짜별 히스토리 응답 모델"""
    date: str
    last_updated: Optional[str] = None
    sessions: list[SessionResponse] = []
    # 해당 날짜 전체 밈 총 감지 횟수 (모든 세션 합계)
    total_ji_chang: int = 0
    total_sesin: int = 0
    total_jjajang: int = 0
    total_djrg: int = 0
    total_sdn: int = 0


class HistoryListResponse(BaseModel):
    """히스토리 목록 응답 모델"""
    available_dates: list[str]
    total_files: int


# ==============================================================================
# 도메인 모델 (Domain Models)
# ==============================================================================

@dataclass
class BroadcastInfo:
    """현재 방송 정보를 담는 데이터 클래스"""
    broadcast_no: str
    title: str
    start_time: Optional[str] = None


@dataclass
class ChatConnectionInfo:
    """채팅 서버 연결 정보를 담는 데이터 클래스"""
    domain: str
    chat_no: str
    ftk: str
    port: str
    broadcaster_id: str
    
    @property
    def websocket_uri(self) -> str:
        """WebSocket 연결 URI 생성"""
        return f"wss://{self.domain}:{self.port}/Websocket/{self.broadcaster_id}"


@dataclass
class HotMoment:
    """Hot Moment 데이터 클래스"""
    time: str
    count: int
    description: str
    detected_memes: list[str] = field(default_factory=list)  # 감지된 밈 목록


class HotMomentRecorder(Protocol):
    """Hot Moment 기록 추상화 (DIP: 핸들러가 구체 클래스가 아닌 이 인터페이스에 의존)"""
    def record_wave_as_hot_moment(
        self, timestamp: datetime, meme_name: str, count: int
    ) -> HotMoment: ...


@dataclass
class BroadcastHistory:
    """방송 기록 데이터 클래스"""
    date: str
    title: str
    ji_chang_waves: int
    sesin_waves: int
    jjajang_waves: int
    djrg_waves: int
    sdn_waves: int


# ==============================================================================
# Wave 감지 로직 (Wave Detection)
# ==============================================================================

@dataclass
class WaveDetectionConfig:
    """웨이브 감지 설정"""
    min_duration_seconds: float = 20.0  # 20초간
    min_message_count: int = 20  # 20개 이상 시 Wave
    gap_timeout_seconds: float = 10.0
    cooldown_seconds: float = 60.0  # Wave 확정 후 쿨다운


class MemeScanner:
    """
    개별 밈의 패턴 매칭 및 Wave(물타기) 감지를 담당합니다.
    
    Wave란?
    -------
    채팅에서 특정 밈이 연속적으로 등장하는 현상입니다.
    20초간 20개 이상의 메시지가 감지되면 하나의 Wave로 카운트됩니다.
    """
    
    __slots__ = (
        "_pattern", "_config", "_compiled_regex",
        "_wave_count", "_total_count",
        "_streak_start", "_streak_last", "_streak_count", "_streak_confirmed",
        "_last_wave_time", "_wave_just_confirmed"  # 웨이브 쿨다운용, Wave→Hot Moment 전달용
    )
    
    def __init__(
        self, 
        pattern: MemePattern,
        config: WaveDetectionConfig | None = None
    ) -> None:
        self._pattern = pattern
        self._config = config or WaveDetectionConfig()
        self._compiled_regex = re.compile(pattern.regex_pattern)
        
        # 통계
        self._wave_count = 0
        self._total_count = 0
        
        # Wave 감지 상태
        self._streak_start: Optional[datetime] = None
        self._streak_last: Optional[datetime] = None
        self._streak_count = 0
        self._streak_confirmed = False
        self._last_wave_time: Optional[datetime] = None  # 마지막 Wave 확정 시간
        self._wave_just_confirmed: Optional[tuple[datetime, int]] = None  # (timestamp, count) Wave→Hot Moment
    
    @property
    def key(self) -> str:
        return self._pattern.key
    
    @property
    def display_name(self) -> str:
        return self._pattern.display_name
    
    @property
    def wave_count(self) -> int:
        return self._wave_count
    
    @property
    def total_count(self) -> int:
        return self._total_count
    
    def reset(self) -> None:
        """새 세션 시작 시 모든 상태를 초기화합니다."""
        self._wave_count = 0
        self._total_count = 0
        self._last_wave_time = None
        self._reset_streak()
    
    def _reset_streak(self) -> None:
        """현재 진행 중인 streak을 초기화합니다."""
        self._streak_start = None
        self._streak_last = None
        self._streak_count = 0
        self._streak_confirmed = False
        self._wave_just_confirmed = None
    
    def process_message(self, message: str, timestamp: datetime) -> bool:
        """
        메시지를 분석하여 밈 패턴을 감지합니다.
        
        Args:
            message: 채팅 메시지
            timestamp: 메시지 수신 시간
            
        Returns:
            밈이 감지되었으면 True, 아니면 False
        """
        if not self._match_pattern(message):
            return False
        
        self._total_count += 1
        self._update_wave_detection(timestamp)
        return True
    
    def _match_pattern(self, message: str) -> bool:
        """메시지에서 밈 패턴을 찾습니다."""
        return bool(self._compiled_regex.search(message))
    
    def _update_wave_detection(self, timestamp: datetime) -> None:
        """Wave 감지 상태를 업데이트합니다."""
        self._check_streak_timeout(timestamp)
        self._start_new_streak_if_needed(timestamp)
        self._increment_streak(timestamp)
        self._check_wave_confirmation(timestamp)
    
    def _check_streak_timeout(self, timestamp: datetime) -> None:
        """streak이 타임아웃되었는지 확인합니다."""
        if self._streak_last is None:
            return
            
        gap = (timestamp - self._streak_last).total_seconds()
        if gap > self._config.gap_timeout_seconds:
            self._reset_streak()
    
    def _start_new_streak_if_needed(self, timestamp: datetime) -> None:
        """필요한 경우 새 streak을 시작합니다."""
        if self._streak_start is None:
            self._streak_start = timestamp
            self._streak_count = 0
            self._streak_confirmed = False
    
    def _increment_streak(self, timestamp: datetime) -> None:
        """streak 카운트를 증가시킵니다."""
        self._streak_count += 1
        self._streak_last = timestamp
    
    def _check_wave_confirmation(self, timestamp: datetime) -> None:
        """Wave 조건을 충족했는지 확인하고, 충족 시 Wave를 확정합니다."""
        if self._streak_confirmed or self._streak_start is None:
            return
        
        # 쿨다운 체크
        if not self._is_cooldown_passed(timestamp):
            return
        
        duration = (timestamp - self._streak_start).total_seconds()
        is_long_enough = duration >= self._config.min_duration_seconds
        is_count_enough = self._streak_count >= self._config.min_message_count
        
        if is_long_enough and is_count_enough:
            self._wave_count += 1
            self._streak_confirmed = True
            self._last_wave_time = timestamp
            self._wave_just_confirmed = (timestamp, self._streak_count)
            self._log_wave_confirmed()
    
    def pop_wave_just_confirmed(self) -> Optional[tuple[datetime, int]]:
        """방금 확정된 Wave가 있으면 (timestamp, count)를 반환하고 초기화합니다. (Hot Moment 기록용)"""
        out = self._wave_just_confirmed
        self._wave_just_confirmed = None
        return out
    
    def _is_cooldown_passed(self, timestamp: datetime) -> bool:
        """웨이브 쿨다운이 지났는지 확인합니다."""
        if self._last_wave_time is None:
            return True
        
        elapsed = (timestamp - self._last_wave_time).total_seconds()
        return elapsed > self._config.cooldown_seconds
    
    def _log_wave_confirmed(self) -> None:
        """Wave 확정 로그를 출력합니다."""
        print(f"🌊 [WAVE] {self.display_name} 20초간 {self._streak_count}회 확정! (시즌 {self._wave_count}회)")


# ==============================================================================
# HTTP 클라이언트 인터페이스 (Dependency Inversion)
# ==============================================================================

class HttpClientInterface(ABC):
    """HTTP 클라이언트 인터페이스"""
    
    @abstractmethod
    async def get_json(self, url: str, headers: dict | None = None) -> dict:
        """GET 요청을 보내고 JSON 응답을 반환합니다."""
        pass
    
    @abstractmethod
    async def post_json(
        self, 
        url: str, 
        data: dict, 
        headers: dict | None = None
    ) -> dict:
        """POST 요청을 보내고 JSON 응답을 반환합니다."""
        pass


class UrllibHttpClient(HttpClientInterface):
    """urllib을 사용한 HTTP 클라이언트 구현"""
    
    __slots__ = ("_user_agent",)
    
    def __init__(self, user_agent: str = USER_AGENT) -> None:
        self._user_agent = user_agent
    
    def _build_headers(self, extra_headers: dict | None = None) -> dict:
        """요청 헤더를 생성합니다."""
        headers = {"User-Agent": self._user_agent}
        if extra_headers:
            headers.update(extra_headers)
        return headers
    
    async def _execute_request(self, request: urllib.request.Request) -> dict:
        """요청을 비동기로 실행합니다."""
        loop = asyncio.get_running_loop()
        
        def fetch():
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read())
        
        return await loop.run_in_executor(None, fetch)
    
    async def get_json(self, url: str, headers: dict | None = None) -> dict:
        """GET 요청을 비동기로 실행합니다."""
        request = urllib.request.Request(url, headers=self._build_headers(headers))
        return await self._execute_request(request)
    
    async def post_json(
        self, 
        url: str, 
        data: dict, 
        headers: dict | None = None
    ) -> dict:
        """POST 요청을 비동기로 실행합니다."""
        encoded_data = urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(
            url, 
            data=encoded_data, 
            headers=self._build_headers(headers)
        )
        return await self._execute_request(request)


# ==============================================================================
# 방송 상태 서비스 (Broadcast Status Service)
# ==============================================================================

class BroadcastStatusService:
    """
    방송 상태 확인을 담당하는 서비스입니다.
    
    아프리카TV Station API를 통해 방송 상태를 조회합니다.
    """
    
    STATION_API_URL = "https://bjapi.afreecatv.com/api/{broadcaster_id}/station"
    
    def __init__(
        self, 
        broadcaster: BroadcasterConfig,
        http_client: HttpClientInterface
    ) -> None:
        self._broadcaster = broadcaster
        self._http_client = http_client
    
    async def get_live_broadcast(self) -> Optional[BroadcastInfo]:
        """
        현재 진행 중인 방송 정보를 조회합니다.
        
        Returns:
            방송 중이면 BroadcastInfo, 아니면 None
        """
        try:
            url = self.STATION_API_URL.format(broadcaster_id=self._broadcaster.id)
            response = await self._http_client.get_json(url)
            return self._parse_broadcast_info(response)
        except Exception:
            return None
    
    def _parse_broadcast_info(self, response: dict) -> Optional[BroadcastInfo]:
        """API 응답에서 방송 정보를 추출합니다. station API: station.broad_start → started_at."""
        broadcast_data = response.get("broad")
        if not broadcast_data:
            return None
        # station API: response["station"]["broad_start"] 값이 started_at에 들어가도록 우선 추출
        station_data = response.get("station") or {}
        start_time = self._extract_broadcast_start_time(station_data)
        if start_time is None:
            start_time = self._extract_broadcast_start_time(broadcast_data)
        if start_time is None:
            start_time = self._extract_broadcast_start_time(response)
        return BroadcastInfo(
            broadcast_no=broadcast_data["broad_no"],
            title=broadcast_data["broad_title"],
            start_time=start_time
        )
    
    def _extract_broadcast_start_time(self, data: dict) -> Optional[str]:
        """broad_start 등 시작 시간 필드를 추출합니다. station API는 broad_start 사용."""
        for key in ("broad_start", "broad_start_time", "start_time"):
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, (int, float)):
                try:
                    dt = datetime.fromtimestamp(int(value))
                    return dt.strftime("%Y-%m-%d %H:%M:%S")
                except (OSError, ValueError):
                    continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None


# ==============================================================================
# 채팅 연결 서비스 (Chat Connection Service)
# ==============================================================================

class ChatConnectionService:
    """
    채팅 서버 연결 정보 조회를 담당하는 서비스입니다.
    
    아프리카TV Player Live API를 통해 채팅 서버 정보를 조회합니다.
    """
    
    PLAYER_API_URL = "https://live.afreecatv.com/afreeca/player_live_api.php"
    
    def __init__(
        self,
        broadcaster: BroadcasterConfig,
        http_client: HttpClientInterface
    ) -> None:
        self._broadcaster = broadcaster
        self._http_client = http_client
    
    async def get_connection_info(
        self, 
        broadcast_no: str
    ) -> Optional[ChatConnectionInfo]:
        """
        채팅 서버 연결 정보를 조회합니다.
        
        Args:
            broadcast_no: 방송 번호
            
        Returns:
            연결 정보 또는 None
        """
        try:
            response = await self._fetch_channel_info(broadcast_no)
            return self._parse_connection_info(response)
        except Exception:
            return None
    
    async def _fetch_channel_info(self, broadcast_no: str) -> dict:
        """채널 정보를 API에서 조회합니다."""
        request_data = {
            "bid": self._broadcaster.id,
            "bno": broadcast_no,
            "type": "live",
            "player_type": "html5"
        }
        return await self._http_client.post_json(self.PLAYER_API_URL, request_data)
    
    def _parse_connection_info(self, response: dict) -> Optional[ChatConnectionInfo]:
        """API 응답에서 연결 정보를 추출합니다."""
        channel = response.get("CHANNEL")
        if not channel:
            return None
        
        return ChatConnectionInfo(
            domain=channel["CHDOMAIN"].lower(),
            chat_no=channel["CHATNO"],
            ftk=channel["FTK"],
            port=str(int(channel["CHPT"]) + 1),
            broadcaster_id=channel["BJID"]
        )


# ==============================================================================
# 채팅 패킷 빌더 (Chat Packet Builder)
# ==============================================================================

class ChatPacketBuilder:
    """채팅 프로토콜 패킷을 생성합니다."""
    
    @staticmethod
    def create_packet(service_type: str, body: str) -> bytes:
        """
        채팅 프로토콜에 맞는 패킷을 생성합니다.
        
        Args:
            service_type: 서비스 타입 코드 (4자리)
            body: 패킷 본문
            
        Returns:
            인코딩된 패킷 바이트
        """
        body_bytes = body.encode("utf-8")
        body_length = len(body_bytes)
        header = f"{service_type}{body_length:06}00"
        
        escape_bytes = ChatProtocol.ESCAPE_SEQUENCE.encode("utf-8")
        header_bytes = header.encode("utf-8")
        
        return escape_bytes + header_bytes + body_bytes
    
    @staticmethod
    def create_login_packet() -> bytes:
        """로그인 패킷을 생성합니다."""
        sep = ChatProtocol.FIELD_SEPARATOR
        body = f"{sep * 3}16{sep}"
        return ChatPacketBuilder.create_packet(ChatProtocol.ServiceType.LOGIN, body)
    
    @staticmethod
    def create_join_packet(chat_no: str, ftk: str) -> bytes:
        """채널 입장 패킷을 생성합니다."""
        sep = ChatProtocol.FIELD_SEPARATOR
        body = f"{sep}{chat_no}{sep}{ftk}{sep}0{sep}{sep}"
        return ChatPacketBuilder.create_packet(ChatProtocol.ServiceType.JOIN, body)
    
    @staticmethod
    def create_ping_packet() -> bytes:
        """핑 패킷을 생성합니다."""
        return ChatPacketBuilder.create_packet(
            ChatProtocol.ServiceType.PING, 
            ChatProtocol.FIELD_SEPARATOR
        )


# ==============================================================================
# 채팅 메시지 파서 (Chat Message Parser)
# ==============================================================================

@dataclass
class ParsedChatMessage:
    """파싱된 채팅 메시지"""
    content: str
    nickname: str
    is_system_message: bool = False


class ChatMessageParser:
    """채팅 메시지를 파싱합니다."""
    
    SYSTEM_MESSAGE_INDICATORS = ("-1", "1")
    
    @staticmethod
    def parse(raw_data: bytes) -> Optional[ParsedChatMessage]:
        """
        원시 데이터를 파싱하여 채팅 메시지를 추출합니다.
        
        Args:
            raw_data: WebSocket에서 수신한 원시 데이터
            
        Returns:
            파싱된 메시지 또는 None
        """
        try:
            decoded = raw_data.decode("utf-8", errors="ignore")
            parts = decoded.split(ChatProtocol.FIELD_SEPARATOR)
            
            if len(parts) < 7:
                return None
            
            command_code = parts[0][2:6]
            if command_code != ChatProtocol.ServiceType.CHATTING:
                return None
            
            message_content = parts[1]
            nickname = parts[6]
            
            is_system = (
                message_content in ChatMessageParser.SYSTEM_MESSAGE_INDICATORS 
                or "fw=" in message_content
            )
            
            return ParsedChatMessage(
                content=message_content,
                nickname=nickname,
                is_system_message=is_system
            )
        except Exception:
            return None


# ==============================================================================
# Hot Moment 감지기 (Hot Moment Detector)
# ==============================================================================

@dataclass
class HotMomentConfig:
    """Hot Moment 저장/표시 설정 (Wave 확정 시 1건 = 1 Hot Moment)"""
    max_history: int = 100
    data_directory: str = "data/hot_moments"


class HotMomentDetector:
    """
    Hot Moment를 기록·저장합니다. (단일 책임: Wave 확정 시 Hot Moment 기록 + 파일 저장)
    
    하나의 Wave = 하나의 Hot Moment. Wave 확정 시 record_wave_as_hot_moment로만 기록됩니다.
    """
    
    __slots__ = ("_config", "_hot_moments", "_last_hot_time")
    
    def __init__(self, config: HotMomentConfig | None = None) -> None:
        self._config = config or HotMomentConfig()
        self._hot_moments: list[HotMoment] = []
        self._last_hot_time: Optional[datetime] = None
        self._ensure_data_directory()
    
    def _ensure_data_directory(self) -> None:
        """데이터 디렉토리가 존재하는지 확인하고 없으면 생성합니다."""
        import os
        os.makedirs(self._config.data_directory, exist_ok=True)
    
    @property
    def hot_moments(self) -> list[HotMoment]:
        return self._hot_moments[:]
    
    def reset(self) -> None:
        """상태를 초기화합니다."""
        self._hot_moments.clear()
        self._last_hot_time = None
    
    def _record_hot_moment(self, hot_moment: HotMoment, timestamp: datetime) -> None:
        """Hot Moment를 기록합니다."""
        self._hot_moments.insert(0, hot_moment)
        self._last_hot_time = timestamp
        
        # 최대 개수 제한
        if len(self._hot_moments) > self._config.max_history:
            self._hot_moments.pop()
    
    def _log_hot_moment(
        self, 
        timestamp: datetime, 
        density: int, 
        detected_memes: list[str]
    ) -> None:
        """Hot Moment 로그를 출력합니다."""
        time_str = timestamp.strftime("%H:%M:%S")
        meme_names = ", ".join(detected_memes) if detected_memes else "알 수 없음"
        print(f"🔥 [HOT] {time_str} - 20초간 {density}회 [{meme_names}] 감지됨!")
    
    def record_wave_as_hot_moment(
        self, 
        timestamp: datetime, 
        meme_name: str, 
        count: int
    ) -> HotMoment:
        """
        Wave 확정 시 하나의 Hot Moment로 기록합니다. (하나의 Wave = 하나의 Hot Moment)
        """
        duration_sec = 20  # Wave 조건과 동일
        description = f"{duration_sec}초간 {count}회 [{meme_name}] 폭주!"
        hot_moment = HotMoment(
            time=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            count=count,
            description=description,
            detected_memes=[meme_name]
        )
        self._record_hot_moment(hot_moment, timestamp)
        self._log_hot_moment(timestamp, count, [meme_name])
        return hot_moment
    
    # ==================== JSON 파일 저장/로드 ====================
    
    def _get_json_filepath(self, date_str: str) -> str:
        """날짜에 해당하는 JSON 파일 경로를 반환합니다."""
        import os
        return os.path.join(self._config.data_directory, f"{date_str}.json")
    
    def save_to_file(
        self,
        broadcast_title: str,
        start_time: Optional[str] = None,
        meme_totals: Optional[dict[str, int]] = None
    ) -> None:
        """
        현재 세션의 hot_moments를 날짜별 JSON 파일에 저장합니다.
        
        Args:
            broadcast_title: 방송 제목
            start_time: 방송 시작 시간 (날짜 추출용)
            meme_totals: 세션별 밈 총 감지 횟수 (ji_chang, sesin, jjajang, djrg, sdn)
        """
        if not self._hot_moments:
            print("📝 저장할 Hot Moment가 없습니다.")
            return
        
        date_str = _extract_date_from_start_time(start_time)
        filepath = self._get_json_filepath(date_str)
        
        # 기존 데이터 로드 (있으면)
        existing_data = self._load_json_file(filepath)
        
        totals = meme_totals or {}
        # 새 방송 세션 데이터 생성
        session_data = {
            "broadcast_title": broadcast_title,
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hot_moments": [
                {
                    "time": moment.time,
                    "count": moment.count,
                    "description": moment.description,
                    "detected_memes": moment.detected_memes
                }
                for moment in self._hot_moments
            ],
            "total_ji_chang": totals.get("ji_chang", 0),
            "total_sesin": totals.get("sesin", 0),
            "total_jjajang": totals.get("jjajang", 0),
            "total_djrg": totals.get("djrg", 0),
            "total_sdn": totals.get("sdn", 0),
        }
        
        # 기존 데이터에 새 세션 추가
        if "sessions" not in existing_data:
            existing_data["sessions"] = []
        
        existing_data["sessions"].append(session_data)
        existing_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing_data["date"] = date_str
        
        # 파일에 저장
        self._save_json_file(filepath, existing_data)
        print(f"💾 Hot Moments 저장 완료: {filepath} ({len(self._hot_moments)}개)")
    
    def _load_json_file(self, filepath: str) -> dict:
        """JSON 파일을 로드합니다. 파일이 없으면 빈 dict를 반환합니다."""
        import os
        
        if not os.path.exists(filepath):
            return {}
        
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️ JSON 파일 로드 실패: {e}")
            return {}
    
    def _save_json_file(self, filepath: str, data: dict) -> None:
        """JSON 파일에 저장합니다."""
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"⚠️ JSON 파일 저장 실패: {e}")


# ==============================================================================
# 방송 기록 관리자 (Broadcast History Manager)
# ==============================================================================

class BroadcastHistoryManager:
    """방송 기록을 관리합니다."""
    
    __slots__ = ("_history",)
    
    MAX_HISTORY = 50
    
    def __init__(self) -> None:
        self._history: list[BroadcastHistory] = []
    
    @property
    def history(self) -> list[BroadcastHistory]:
        return self._history[:]
    
    def save_broadcast(
        self,
        title: str,
        start_time: Optional[str],
        scanners: dict[str, MemeScanner]
    ) -> None:
        """
        방송 기록을 저장합니다.
        
        Args:
            title: 방송 제목
            start_time: 방송 시작 시간 문자열
            scanners: 밈 스캐너 딕셔너리
        """
        date_str = _extract_date_from_start_time(start_time)
        
        record = BroadcastHistory(
            date=date_str,
            title=title,
            ji_chang_waves=scanners["ji_chang"].wave_count,
            sesin_waves=scanners["sesin"].wave_count,
            jjajang_waves=scanners["jjajang"].wave_count,
            djrg_waves=scanners["djrg"].wave_count,
            sdn_waves=scanners["sdn"].wave_count
        )
        
        self._add_record(record)
        self._log_saved(date_str, scanners["ji_chang"].wave_count)
    
    def _add_record(self, record: BroadcastHistory) -> None:
        """기록을 추가하고 최대 개수를 유지합니다."""
        self._history.insert(0, record)
        if len(self._history) > self.MAX_HISTORY:
            self._history.pop()
    
    def _log_saved(self, date_str: str, ji_chang_waves: int) -> None:
        """저장 완료 로그를 출력합니다."""
        print(f"✅ 리포트 저장 완료: {date_str} (Ji-Chang Waves: {ji_chang_waves})")


# ==============================================================================
# 폴링 전략 (Polling Strategy)
# ==============================================================================

class PollingStrategy:
    """
    방송 상태 확인 주기를 결정하는 전략 클래스입니다.
    
    상황에 따라 다른 폴링 주기를 반환합니다:
    - 방송 중: 60초
    - 방송 종료 직후 (9분 이내): 180초 (리방 대비)
    - 피크 타임 (16~18시): 180초
    - 기본: 600초
    """
    
    POLLING_LIVE = 60
    POLLING_AFTER_END = 180
    POLLING_PEAK_TIME = 180
    POLLING_DEFAULT = 600
    
    AFTER_END_WINDOW_SECONDS = 540  # 9분
    PEAK_HOUR_START = 16
    PEAK_HOUR_END = 18
    
    @staticmethod
    def get_interval(
        is_live: bool,
        last_stream_end_time: Optional[datetime],
        current_time: datetime
    ) -> int:
        """
        현재 상황에 맞는 폴링 주기를 반환합니다.
        
        Args:
            is_live: 현재 방송 중인지 여부
            last_stream_end_time: 마지막 방송 종료 시간
            current_time: 현재 시간
            
        Returns:
            폴링 주기 (초)
        """
        if is_live:
            return PollingStrategy.POLLING_LIVE
        
        if PollingStrategy._is_shortly_after_end(last_stream_end_time, current_time):
            return PollingStrategy.POLLING_AFTER_END
        
        if PollingStrategy._is_peak_time(current_time):
            return PollingStrategy.POLLING_PEAK_TIME
        
        return PollingStrategy.POLLING_DEFAULT
    
    @staticmethod
    def _is_shortly_after_end(
        last_end_time: Optional[datetime],
        current_time: datetime
    ) -> bool:
        """방송 종료 직후인지 확인합니다."""
        if last_end_time is None:
            return False
        
        elapsed = (current_time - last_end_time).total_seconds()
        return elapsed < PollingStrategy.AFTER_END_WINDOW_SECONDS
    
    @staticmethod
    def _is_peak_time(current_time: datetime) -> bool:
        """피크 타임인지 확인합니다."""
        return PollingStrategy.PEAK_HOUR_START <= current_time.hour < PollingStrategy.PEAK_HOUR_END


# ==============================================================================
# WebSocket 채팅 핸들러 (WebSocket Chat Handler)
# ==============================================================================

class ChatWebSocketHandler:
    """WebSocket을 통한 채팅 연결을 관리합니다. (DIP: HotMomentRecorder 프로토콜에 의존)"""
    
    PING_INTERVAL_SECONDS = 20
    
    def __init__(
        self,
        scanners: dict[str, MemeScanner],
        hot_moment_recorder: HotMomentRecorder,
    ) -> None:
        self._scanners = scanners
        self._hot_moment_recorder = hot_moment_recorder
        self._last_detected_at: Optional[datetime] = None
    
    @property
    def last_detected_at(self) -> Optional[datetime]:
        return self._last_detected_at
    
    @last_detected_at.setter
    def last_detected_at(self, value: Optional[datetime]) -> None:
        self._last_detected_at = value
    
    async def connect_and_listen(self, connection_info: ChatConnectionInfo) -> None:
        """
        채팅 서버에 연결하고 메시지를 수신합니다.
        
        Args:
            connection_info: 연결 정보
        """
        uri = connection_info.websocket_uri
        print(f"🔗 채팅 서버 연결: {uri}")
        
        ssl_context = self._create_ssl_context()
        
        try:
            async with websockets.connect(
                uri, 
                subprotocols=["chat"], 
                ssl=ssl_context, 
                ping_interval=None
            ) as websocket:
                await self._perform_handshake(websocket, connection_info)
                await self._listen_for_messages(websocket)
        except Exception as e:
            print(f"⚠️ WS 접속 실패: {e}")
    
    def _create_ssl_context(self) -> ssl.SSLContext:
        """SSL 컨텍스트를 생성합니다."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    
    async def _perform_handshake(
        self, 
        websocket, 
        connection_info: ChatConnectionInfo
    ) -> None:
        """로그인 및 채널 입장 핸드셰이크를 수행합니다."""
        # 로그인
        await websocket.send(ChatPacketBuilder.create_login_packet())
        await websocket.recv()
        
        # 채널 입장
        join_packet = ChatPacketBuilder.create_join_packet(
            connection_info.chat_no,
            connection_info.ftk
        )
        await websocket.send(join_packet)
        
        print("✅ 채팅 감시 시작...")
    
    async def _listen_for_messages(self, websocket) -> None:
        """메시지 수신 루프를 실행합니다."""
        ping_task = asyncio.create_task(self._send_periodic_pings(websocket))
        
        try:
            async for raw_data in websocket:
                await self._handle_raw_message(raw_data)
        except Exception as e:
            print(f"❌ WS 연결 끊김: {e}")
        finally:
            ping_task.cancel()
    
    async def _send_periodic_pings(self, websocket) -> None:
        """주기적으로 핑을 전송합니다."""
        try:
            while True:
                await asyncio.sleep(self.PING_INTERVAL_SECONDS)
                await websocket.send(ChatPacketBuilder.create_ping_packet())
        except asyncio.CancelledError:
            pass
    
    async def _handle_raw_message(self, raw_data: bytes) -> None:
        """원시 메시지를 처리합니다."""
        parsed = ChatMessageParser.parse(raw_data)
        
        if parsed is None or parsed.is_system_message:
            return
        
        now = datetime.now()
        detected_memes = self._process_with_scanners(parsed.content, now)
        
        if detected_memes:
            self._last_detected_at = now
        
        # 하나의 Wave = 하나의 Hot Moment: Wave 확정 시 Hot Moment로 기록
        for scanner in self._scanners.values():
            wave_info = scanner.pop_wave_just_confirmed()
            if wave_info is not None:
                ts, count = wave_info
                self._hot_moment_recorder.record_wave_as_hot_moment(ts, scanner.display_name, count)
    
    def _process_with_scanners(self, message: str, timestamp: datetime) -> list[str]:
        """모든 스캐너로 메시지를 처리하고 감지된 밈 목록을 반환합니다."""
        detected_memes = []
        for scanner in self._scanners.values():
            if scanner.process_message(message, timestamp):
                detected_memes.append(scanner.display_name)
        return detected_memes


# ==============================================================================
# 메인 모니터링 봇 (Main Monitor Bot)
# ==============================================================================

@dataclass
class BotState:
    """봇의 현재 상태를 담는 데이터 클래스"""
    is_live: bool = False
    current_broadcast_no: Optional[str] = None
    broadcast_title: str = "방송 준비 중"
    broadcast_start_time: Optional[str] = None
    last_stream_end_time: Optional[datetime] = None
    websocket_task: Optional[asyncio.Task] = None


class AutoMonitorBot:
    """
    방송 감지 및 채팅 모니터링 메인 컨트롤러입니다.
    
    이 클래스는 다음을 조율합니다:
    - 방송 상태 폴링
    - 세션 시작/종료
    - 채팅 모니터링
    - 통계 수집
    """
    
    def __init__(
        self,
        broadcaster: BroadcasterConfig = TARGET_BROADCASTER,
        http_client: Optional[HttpClientInterface] = None
    ) -> None:
        self._broadcaster = broadcaster
        self._http_client = http_client or UrllibHttpClient()
        
        # 서비스 초기화
        self._broadcast_service = BroadcastStatusService(broadcaster, self._http_client)
        self._chat_service = ChatConnectionService(broadcaster, self._http_client)
        
        # 스캐너 초기화
        self._scanners = {
            pattern.key: MemeScanner(pattern)
            for pattern in MEME_PATTERNS
        }
        
        # 감지기 및 관리자
        self._hot_moment_detector = HotMomentDetector()
        self._history_manager = BroadcastHistoryManager()
        
        # 채팅 핸들러
        self._chat_handler = ChatWebSocketHandler(
            self._scanners,
            self._hot_moment_detector
        )
        
        # 상태
        self._state = BotState()
    
    # 프로퍼티 (외부 인터페이스)
    @property
    def is_live(self) -> bool:
        return self._state.is_live
    
    @property
    def broadcast_title(self) -> str:
        return self._state.broadcast_title
    
    @property
    def broadcast_start_time(self) -> Optional[str]:
        return self._state.broadcast_start_time
    
    @property
    def scanners(self) -> dict[str, MemeScanner]:
        return self._scanners
    
    @property
    def last_detected_at(self) -> Optional[datetime]:
        return self._chat_handler.last_detected_at
    
    @property
    def hot_moments(self) -> list[HotMoment]:
        return self._hot_moment_detector.hot_moments
    
    @property
    def history(self) -> list[BroadcastHistory]:
        return self._history_manager.history
    
    async def run_forever(self) -> None:
        """메인 실행 루프를 시작합니다."""
        self._log_startup()
        
        while True:
            try:
                await self._check_and_update_status()
                sleep_time = self._calculate_sleep_time()
                await asyncio.sleep(sleep_time)
            except Exception as e:
                print(f"⚠️ 루프 에러: {e}")
                await asyncio.sleep(60)
    
    def _log_startup(self) -> None:
        """시작 로그를 출력합니다."""
        print(
            f"🤖 [{self._broadcaster.name}] 스마트 감지 봇 가동 "
            f"(ID: {self._broadcaster.id})"
        )
    
    async def _check_and_update_status(self) -> None:
        """방송 상태를 확인하고 업데이트합니다."""
        broadcast_info = await self._broadcast_service.get_live_broadcast()
        
        if broadcast_info:
            await self._handle_live_broadcast(broadcast_info)
        else:
            await self._handle_offline()
    
    async def _handle_live_broadcast(self, broadcast_info: BroadcastInfo) -> None:
        """방송 중 상태를 처리합니다."""
        is_new_broadcast = (
            not self._state.is_live 
            or self._state.current_broadcast_no != broadcast_info.broadcast_no
        )
        
        if is_new_broadcast:
            print(f"\n📺 방송 시작 감지! ({broadcast_info.title})")
            await self._start_session(broadcast_info)
    
    async def _handle_offline(self) -> None:
        """오프라인 상태를 처리합니다."""
        if self._state.is_live:
            print(f"\n💤 방송 종료 감지. ({datetime.now().strftime('%H:%M:%S')})")
            await self._stop_session()
            self._state.last_stream_end_time = datetime.now()
    
    def _calculate_sleep_time(self) -> int:
        """다음 폴링까지의 대기 시간을 계산합니다."""
        return PollingStrategy.get_interval(
            self._state.is_live,
            self._state.last_stream_end_time,
            datetime.now()
        )
    
    async def _start_session(self, broadcast_info: BroadcastInfo) -> None:
        """새 방송 세션을 시작합니다."""
        await self._stop_session()
        
        self._update_state_for_new_session(broadcast_info)
        self._reset_session_data()
        await self._connect_to_chat()
    
    def _update_state_for_new_session(self, broadcast_info: BroadcastInfo) -> None:
        """새 세션을 위해 상태를 업데이트합니다."""
        self._state.is_live = True
        self._state.current_broadcast_no = broadcast_info.broadcast_no
        self._state.broadcast_title = broadcast_info.title
        # API에서 시작 시간이 없으면 세션 시작 시각을 사용 (started_at이 null이 되지 않도록)
        self._state.broadcast_start_time = (
            broadcast_info.start_time
            or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    
    def _reset_session_data(self) -> None:
        """세션 데이터를 초기화합니다."""
        for scanner in self._scanners.values():
            scanner.reset()
        
        self._chat_handler.last_detected_at = None
        self._hot_moment_detector.reset()
    
    async def _connect_to_chat(self) -> None:
        """채팅 서버에 연결합니다."""
        connection_info = await self._chat_service.get_connection_info(
            self._state.current_broadcast_no
        )
        
        if connection_info:
            self._state.websocket_task = asyncio.create_task(
                self._chat_handler.connect_and_listen(connection_info)
            )
    
    async def _stop_session(self) -> None:
        """현재 세션을 종료합니다."""
        if self._state.is_live:
            self._save_broadcast_history()
        
        self._reset_state()
        self._cancel_websocket_task()
    
    def _save_broadcast_history(self) -> None:
        """방송 기록을 저장합니다."""
        # 방송 기록 저장
        self._history_manager.save_broadcast(
            self._state.broadcast_title,
            self._state.broadcast_start_time,
            self._scanners
        )
        
        # Hot Moments JSON 파일로 저장 (밈별 총 횟수 포함)
        meme_totals = {key: scanner.total_count for key, scanner in self._scanners.items()}
        self._hot_moment_detector.save_to_file(
            self._state.broadcast_title,
            self._state.broadcast_start_time,
            meme_totals=meme_totals
        )
    
    def _reset_state(self) -> None:
        """상태를 초기화합니다."""
        self._state.is_live = False
        self._state.current_broadcast_no = None
        self._state.broadcast_title = "방송 준비 중"
    
    def _cancel_websocket_task(self) -> None:
        """WebSocket 태스크를 취소합니다."""
        if self._state.websocket_task:
            self._state.websocket_task.cancel()
            self._state.websocket_task = None


# ==============================================================================
# FastAPI 애플리케이션
# ==============================================================================

def create_app() -> FastAPI:
    """FastAPI 애플리케이션을 생성합니다."""
    application = FastAPI(title="SOOP 지피티 지창 봇")
    bot = AutoMonitorBot()
    
    @application.on_event("startup")
    async def startup_event():
        asyncio.create_task(bot.run_forever())
    
    @application.get("/")
    async def root():
        return {
            "message": f"Soop Ji-Chang Bot for {TARGET_BROADCASTER.name} is Running."
        }
    
    @application.get("/health")
    async def health_check():
        return {"status": "ok"}
    
    @application.get("/stats", response_model=StatsResponse)
    async def get_stats():
        return _build_stats_response(bot)
    
    @application.get("/history")
    async def get_history_list():
        """data/hot_moments/ 디렉토리의 사용 가능한 날짜 목록을 반환합니다."""
        return _get_available_history_dates()
    
    @application.get("/history/{date}")
    async def get_history_by_date(date: str):
        """특정 날짜의 Hot Moments 데이터를 반환합니다."""
        return _load_history_file(date)
    
    return application


def _build_stats_response(bot: AutoMonitorBot) -> StatsResponse:
    """통계 응답을 생성합니다."""
    return StatsResponse(
        status="LIVE" if bot.is_live else "WAITING",
        broadcast_title=bot.broadcast_title,
        started_at=bot.broadcast_start_time,
        
        # 밈별 통계
        ji_chang_wave_count=bot.scanners["ji_chang"].wave_count,
        total_ji_chang_chat_count=bot.scanners["ji_chang"].total_count,
        
        sesin_wave_count=bot.scanners["sesin"].wave_count,
        total_sesin_chat_count=bot.scanners["sesin"].total_count,
        
        jjajang_wave_count=bot.scanners["jjajang"].wave_count,
        total_jjajang_chat_count=bot.scanners["jjajang"].total_count,
        
        djrg_wave_count=bot.scanners["djrg"].wave_count,
        total_djrg_chat_count=bot.scanners["djrg"].total_count,
        
        sdn_wave_count=bot.scanners["sdn"].wave_count,
        total_sdn_chat_count=bot.scanners["sdn"].total_count,
        
        last_detected_at=bot.last_detected_at,
        hot_moments=[
            HotMomentResponse(
                time=moment.time,
                count=moment.count,
                description=moment.description
            )
            for moment in bot.hot_moments
        ],
        history=[
            BroadcastHistoryResponse(
                date=record.date,
                title=record.title,
                total_ji_chang=record.ji_chang_waves,
                total_sesin=record.sesin_waves,
                total_jjajang=record.jjajang_waves,
                total_djrg=record.djrg_waves,
                total_sdn=record.sdn_waves
            )
            for record in bot.history
        ]
    )


# Hot Moments 히스토리 관련 상수
HOT_MOMENTS_DIR = "data/hot_moments"


def _get_available_history_dates() -> HistoryListResponse:
    """사용 가능한 히스토리 날짜 목록을 반환합니다."""
    import os
    
    if not os.path.exists(HOT_MOMENTS_DIR):
        return HistoryListResponse(available_dates=[], total_files=0)
    
    files = []
    for filename in os.listdir(HOT_MOMENTS_DIR):
        if filename.endswith(".json"):
            # 파일명에서 날짜 추출 (YYYY-MM-DD.json)
            date = filename.replace(".json", "")
            files.append(date)
    
    # 날짜 내림차순 정렬 (최신순)
    files.sort(reverse=True)
    
    return HistoryListResponse(
        available_dates=files,
        total_files=len(files)
    )


def _load_history_file(date: str) -> DailyHistoryResponse:
    """특정 날짜의 히스토리 파일을 로드합니다."""
    import os
    
    filepath = os.path.join(HOT_MOMENTS_DIR, f"{date}.json")
    
    if not os.path.exists(filepath):
        return DailyHistoryResponse(
            date=date,
            last_updated=None,
            sessions=[]
        )
    
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        sessions = []
        daily_ji_chang = daily_sesin = daily_jjajang = daily_djrg = daily_sdn = 0
        for session_data in data.get("sessions", []):
            hot_moments = [
                HotMomentFileResponse(
                    time=hm["time"],
                    count=hm["count"],
                    description=hm["description"],
                    detected_memes=hm.get("detected_memes", [])
                )
                for hm in session_data.get("hot_moments", [])
            ]
            sj = session_data.get("total_ji_chang", 0)
            ss = session_data.get("total_sesin", 0)
            sjj = session_data.get("total_jjajang", 0)
            sd = session_data.get("total_djrg", 0)
            sn = session_data.get("total_sdn", 0)
            daily_ji_chang += sj
            daily_sesin += ss
            daily_jjajang += sjj
            daily_djrg += sd
            daily_sdn += sn
            sessions.append(SessionResponse(
                broadcast_title=session_data.get("broadcast_title", ""),
                saved_at=session_data.get("saved_at", ""),
                hot_moments=hot_moments,
                total_ji_chang=sj,
                total_sesin=ss,
                total_jjajang=sjj,
                total_djrg=sd,
                total_sdn=sn
            ))
        
        return DailyHistoryResponse(
            date=data.get("date", date),
            last_updated=data.get("last_updated"),
            sessions=sessions,
            total_ji_chang=daily_ji_chang,
            total_sesin=daily_sesin,
            total_jjajang=daily_jjajang,
            total_djrg=daily_djrg,
            total_sdn=daily_sdn
        )
    
    except (json.JSONDecodeError, IOError):
        return DailyHistoryResponse(
            date=date,
            last_updated=None,
            sessions=[]
        )


# 앱 인스턴스 생성
app = create_app()


if __name__ == "__main__":
    uvicorn.run("soop_chat_ji:app", host="0.0.0.0", port=8000, reload=True)
