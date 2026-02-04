# 🎬 SOOP 지피티 채팅 모니터링 봇

SOOP(아프리카TV) 방송의 채팅을 실시간으로 모니터링하여 밈 패턴을 감지하고 통계를 수집하는 봇입니다.

## 📌 주요 기능

### 1. 방송 상태 자동 감지
- 아프리카TV API로 LIVE 상태 자동 확인
- 폴링 주기 최적화 (방송 중 60초, 기본 600초, 피크타임 180초)
- 방송 시작/종료 시 자동 세션 관리

### 2. 밈 패턴 감지 (4종류)

| 밈 | 패턴 | 예시 |
|-----|------|------|
| **지창** | `지[ㅡ\s~-]*창` | 지창, 지~창, 지ㅡ창 |
| **세신** | `세[ㅡ\s~-]*신` | 세신, 세~신 |
| **짜장면** | `짜[ㅡ\s~-]*장[ㅡ\s~-]*면` | 짜장면, 짜~장면 |
| **ㄷㅈㄹㄱ** | `ㄷ[ㅡ\s~-]*ㅈ[ㅡ\s~-]*ㄹ[ㅡ\s~-]*ㄱ` | ㄷㅈㄹㄱ |

### 3. Wave(물타기) 감지

| 조건 | 값 |
|------|-----|
| 지속 시간 | 10초 이상 |
| 메시지 수 | 20개 이상 |
| 쿨다운 | 60초 |

### 4. Hot Moment(급증 구간) 감지

| 조건 | 값 |
|------|-----|
| 윈도우 | 10초 |
| 임계값 | 개별 밈 20회 이상 |
| 쿨다운 | 60초 |

### 5. 데이터 저장
- 방송 종료 시 `data/hot_moments/YYYY-MM-DD.json`에 저장
- 날짜별 Hot Moments 기록 관리

---

## 🌐 API 엔드포인트

| 엔드포인트 | 설명 |
|------------|------|
| `GET /` | 봇 실행 상태 확인 |
| `GET /health` | 헬스체크 |
| `GET /stats` | 현재 세션 통계 조회 |
| `GET /history` | 날짜 목록 조회 |
| `GET /history/{date}` | 특정 날짜 Hot Moments 조회 |

### `/stats` 응답 예시

```json
{
  "status": "LIVE",
  "broadcast_title": "지피티",
  "started_at": "2026-02-03 19:17:30",
  "ji_chang_wave_count": 5,
  "total_ji_chang_chat_count": 728,
  "sesin_wave_count": 0,
  "total_sesin_chat_count": 5,
  "hot_moments": [
    {
      "time": "2026-02-03 12:32:51",
      "count": 20,
      "description": "10초간 20회 [지창] 폭주!"
    }
  ]
}
```

---

## 🚀 실행 방법

### 로컬 실행

```bash
python soop_chat_ji.py
```

### Fly.io 배포

```bash
fly deploy
```

---

## 🛠️ VOD 링크 생성기

Hot Moments 시간을 기반으로 VOD 타임스탬프 링크를 생성합니다.

### 사용법

```bash
python vod_link_generator.py
```

### 설정 파일

- `data/timestamp.json`: 밈 감지 데이터
- `data/vod_config.json`: VOD 세그먼트 설정

---

## 📦 프로젝트 구조

```
soop-chatting-crawling/
├── soop_chat_ji.py          # 메인 봇 코드
├── vod_link_generator.py    # VOD 링크 생성기
├── data/
│   ├── hot_moments/         # Hot Moments JSON 저장
│   ├── timestamp.json       # 밈 감지 데이터
│   └── vod_config.json      # VOD 설정
├── Dockerfile
├── fly.toml
└── README.md
```

---

## 📋 기술 스택

- **언어**: Python 3.11+
- **프레임워크**: FastAPI
- **비동기**: asyncio, websockets
- **호스팅**: Fly.io (무료 티어)

---

## 🔧 설정값

| 항목 | 값 |
|------|-----|
| Wave 조건 | 10초 동안 20개 + 60초 쿨다운 |
| Hot Moment 조건 | 10초 동안 개별 밈 20개 + 60초 쿨다운 |
| 폴링 주기 (방송 중) | 60초 |
| 폴링 주기 (기본) | 600초 |

---

## 📝 라이선스

MIT License
