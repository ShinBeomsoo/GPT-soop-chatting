# 스마트 클립 저장소 (Smart Clip Archive) 개발 플랜

## 1. 프로젝트 개요 (Overview)
*   **프로젝트명**: Smart Clip Archive (스마트 클립 저장소) - Backend Logic
*   **목표**: 실시간 라이브 방송 중 시청자의 반응(채팅, 후원)이 폭발하는 순간을 자동으로 감지하여, '지력사무소'에서 활용할 수 있는 **타임스탬프 데이터(JSON)**를 생성합니다.
*   **핵심 가치**: "오늘 방송 킬포(Kill Point)가 어디야?"라는 팬들의 니즈를 충족시키고, 방송 요약 콘텐츠를 자동화합니다.

---

## 2. 핵심 기능 및 알고리즘 (Core Features)

### A. 리액션 감지 엔진 (Reaction Engine)
단순 채팅량 증가가 아닌, 의미 있는 '반응'을 감지하기 위한 알고리즘입니다.

1.  **웃음 감지 (Laugh RPM - Reaction Per Minute)**
    *   **감지 키워드**: `ㅋ`, `z`, `ㅎ` (한국어/영어권 웃음 표현)
    *   **알고리즘**: 슬라이딩 윈도우 (Sliding Window)
        *   최근 `N`초(예: 10초) 동안 버퍼에 쌓인 채팅 중 '웃음 키워드'가 포함된 채팅의 비율과 속도를 계산합니다.
    *   **트리거 조건**:
        *   순간 가속도: 직전 10초 대비 채팅량이 2배 이상 급증.
        *   웃음 밀도: 전체 채팅 중 60% 이상이 웃음 키워드 포함.
    
2.  **후원 감지 (Donation Event)**
    *   **로직**: 별풍선 패킷(`0018`) 감지 시 작동.
    *   **트리거 조건**: 단일 후원 `N`개 이상 또는 연속된 후원 콤보(Combo) 발생 시.

### B. 스마트 타임스탬프 (Smart Timestamp)
*   **문제점**: 라이브 중인 현재 시간(`Wall Clock Time`)과 다시보기(VOD)의 타임라인(`Video Time`)은 다릅니다.
*   **해결책**:
    *   방송 시작 시간(`Broadcast Start Time`)을 API로 수집.
    *   `Event Time` - `Broadcast Start Time` = `VOD Seeker Time` (재생 시점).
    *   이 정보를 기반으로 바로 클릭 가능한 URL 생성 (예: `&t=1h30m20s`).

---

## 3. 기술 아키텍처 (Architecture)

### A. 데이터 수집기 (Collector & Analyzer) - Python
기존 `soop_chat.py`의 구조를 확장하여 분석 모듈을 탑재합니다.

*   `ChatStream`: 웹소켓 데이터를 수신 (Producer).
*   `AnalysisWorker`: 수신된 데이터를 실시간 분석 (Consumer).
    *   내부에 `ReactionBuffer`(deque)를 유지하여 실시간 RPM 계산.
*   `EventLogger`: 감지된 이벤트를 영구 저장소에 기록.

### B. 데이터 저장 포맷 (JSON Structure)
'지력사무소' 서비스에서 바로 파싱하여 사용할 수 있도록 표준화된 JSON 로그를 생성합니다.

```json
{
  "meta": {
    "bj_id": "target_bj",
    "broad_no": "123456789",
    "start_time": "2024-05-20T20:00:00", 
    "vod_base_url": "https://vod.sooplive.co.kr/player/12345678" 
  },
  "events": [
    {
      "id": "evt_1716195600",
      "type": "LAUGH",
      "timestamp_code": "01:05:23", 
      "seconds_offset": 3923,
      "prev_context_seconds": 15,
      "rpm_score": 120,
      "best_chat": "ㅋㅋㅋㅋㅋㅋ 와 대박이다",
      "full_url": "https://vod....&t=3908" 
    },
    {
      "id": "evt_1716195800",
      "type": "DONATION",
      "amount": 100,
      "donor": "팬클럽회장",
      "seconds_offset": 4123
    }
  ]
}
```

---

## 4. 단계별 구현 계획 (Roadmap)

### Phase 1: 리액션 분석기 구현 (Reaction Analyzer)
*   [ ] `soop_chat.py` 확장: `ReactionAnalyzer` 클래스 추가.
*   [ ] **슬라이딩 윈도우 알고리즘**: 최근 10초간의 채팅 데이터를 메모리에 유지하며 RPM(분당 반응) 및 웃음 밀도를 0.1초 단위로 계산.
*   [ ] **쿨타임 로직**: 한 번 이벤트가 감지(Trigger)되면, 이후 30초간은 중복 감지 방지.

### Phase 2: 데이터 동기화 및 저장 (Sync & Logging)
*   [ ] **방송 시작 시간 추적**: 라이브 API를 주기적으로 호출하여 정확한 `BroadStartTime` 획득.
*   [ ] **실시간 파일 쓰기**: 이벤트 발생 시 `logs/clip_data_{date}.json` 파일에 즉시 append (File I/O 최적화).
*   [ ] **지력사무소 연동 테스트**: 생성된 JSON 파일 형식이 기존 시스템과 호환되는지 검증.

---

## 5. 예상 이슈 및 해결 방안

1.  **방송 재시작 이슈 (Rebang)**
    *   **해결**: 방송 번호(`BroadNo`)가 변경되면 새로운 JSON 파일을 생성하여 파일이 섞이는 것을 방지.
2.  **타임스탬프 오차 (Drift)**
    *   **해결**: 서버 시간과 방송 시간의 미세한 오차를 보정하기 위해, 이벤트 감지 시점보다 **10~15초 앞선(Pre-roll)** 시간을 기록.
