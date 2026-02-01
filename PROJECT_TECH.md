# SOOP Chatting Crawling - 기술 분석 및 최적화 보고서

본 문서는 `soop_chat.py` 프로젝트의 핵심 기술 구현 방식과 서버 리소스 절감을 위한 최적화 전략을 상세히 기술합니다.

---

## 1. SOOP 웹소켓 데이터 파싱 (Reverse Engineering)

SOOP(아프리카TV)의 채팅 시스템은 HTTP가 아닌 **TCP/WebSocket**을 통해 바이너리/텍스트 혼합 패킷으로 통신합니다. 이를 브라우저 없이 직접 구현하기 위해 프로토콜을 역공학(Reverse Engineering)하여 구현했습니다.

### A. 패킷 패킹 및 핸드쉐이크 (Handshake)

서버와 통신하기 위해서는 데이터 앞에 **12자리 헤더**를 붙여야 합니다.

```python
# F = 구분자 (\x0c)
# ESC = 이스케이프 (\x1b\t)
header = f"{service_type}{len(body_bytes):06}00"
packet = ESC + header + body_bytes
```

1.  **CONNECT**: 웹소켓 연결 (SSL 적용).
2.  **LOGIN (0001)**: 최초 접속 시 불필요한 데이터를 제외한 간소화된 로그인 패킷 전송.
3.  **JOIN (0002)**: `broadcast_info`에서 획득한 `CHATNO`(채팅방번호)와 `FTK`(입장 토큰)를 사용하여 입장 패킷 전송. 이 단계가 성공해야 실시간 데이터를 수신할 수 있습니다.

### B. 데이터 구조 파싱 (Parsing Logic)

수신된 데이터는 바이트 스트림 형태이며, `\x0c` (Form Feed) 제어 문자를 구분자(Delimiter)로 사용하여 데이터를 쪼갭니다(`split`).

#### 1. 일반 채팅 (ServiceType: 0005)
```
[Header] | [Message] | [UID] | [Nickname] | [Flags] ...
```
-   **메시지**: `parts[1]`
-   **닉네임**: `parts[6]`
-   **유저 권한**: `parts[7]` (비트 플래그)

#### 2. 별풍선 후원 (ServiceType: 0018)
```
[Header] | [UID] | ... | [Nickname] | [Count] ...
```
-   **닉네임**: `parts[3]`
-   **개수**: `parts[4]` (기존 0012 패킷 분석과 달리 0018에서는 인덱스 4번에 위치함)

---

## 2. 서버 비용 절감을 위한 최적화 (Optimization Logic)

이 프로젝트는 월 $0 (Oracle Free Tier) 또는 월 5,000원 이하의 초저사양 VPS에서도 안정적으로 동작하도록 설계되었습니다.

### A. 의존성 제거 (Zero-Dependency)
-   **변경 전**: `requests` 라이브러리 사용 (별도 설치 필요, 무거운 의존성).
-   **변경 후**: `urllib` (Python 표준 라이브러리) 사용.
-   **이점**: 컨테이너 이미지 크기 감소, 설치 속도 향상, 메모리 오버헤드 감소.

### B. 메모리 폭증 방지 (Worker Pattern)
-   **문제점**: 채팅이 초당 1,000개 이상 쏟아지는 대형 방송(Burning Mode) 시, 각 패킷마다 `asyncio.create_task`를 생성하면 **OOM(Out Of Memory)** 발생 위험.
-   **해결책**: **Producer-Consumer 패턴** 도입.
    -   `ws.recv()`는 데이터를 받아 `asyncio.Queue`에 넣기만 함 (Producer).
    -   3개의 고정된 **Worker**가 큐에서 하나씩 꺼내 처리 (Consumer).
-   **효과**: 입력 부하가 처리 속도를 초과해도 메모리가 큐 크기만큼만 사용되며, CPU 점유율이 일정하게 유지됨.

### C. CPU 연산 최적화 (Caching)
-   **적용 대상**: `parse_flags` (유저 뱃지 파싱).
-   **기법**: `functools.lru_cache(maxsize=1024)` 적용.
-   **이유**: 방송 중 채팅하는 사람은 소수의 헤비 유저가 대부분입니다. 동일한 유저가 채팅할 때마다 비트 연산과 문자열 포맷팅을 반복하는 것은 낭비입니다.
-   **효과**: 자주 보이는 시청자의 뱃지 파싱 연산을 O(1)로 단축.

---

## 3. 결론

이 코드는 단순한 스크립트가 아니라, **대규모 트래픽을 견딜 수 있는 구조(Queue, Caching)**와 **최소한의 리소스(Standard Lib)**로 설계된 고효율 크롤링 엔진입니다.
