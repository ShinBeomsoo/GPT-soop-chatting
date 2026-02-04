"""
VOD 타임스탬프 링크 생성기 (다중 VOD 지원)
==========================================

방송이 여러 VOD로 나뉘어진 경우를 지원합니다.

사용법:
    python vod_link_generator.py

데이터 소스:
    - data/timestamp.json: 밈 감지 데이터
    - data/vod_config.json: VOD 설정 (여러 VOD 세그먼트)
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


# 설정
DATA_DIR = Path(__file__).parent / "data"
TIMESTAMP_FILE = DATA_DIR / "timestamp.json"
VOD_CONFIG_FILE = DATA_DIR / "vod_config.json"

# KST 오프셋
KST_OFFSET = timedelta(hours=9)


@dataclass
class VodSegment:
    """VOD 세그먼트 정보"""
    url: str
    start_time: datetime  # KST
    end_time: Optional[datetime] = None  # KST, None이면 마지막 세그먼트


def parse_datetime(datetime_str: str) -> datetime:
    """날짜/시간 문자열을 datetime 객체로 변환합니다."""
    return datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")


def format_time_display(seconds: int) -> str:
    """초를 HH:MM:SS 형식으로 변환합니다."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def generate_vod_link(base_url: str, seconds: int) -> str:
    """타임스탬프가 포함된 VOD 링크를 생성합니다."""
    base_url = base_url.split('?')[0]
    return f"{base_url}?change_second={seconds}"


def load_timestamp_data() -> list[dict]:
    """data/timestamp.json에서 밈 데이터를 로드합니다."""
    if not TIMESTAMP_FILE.exists():
        print(f"❌ 파일을 찾을 수 없습니다: {TIMESTAMP_FILE}")
        return []
    
    try:
        with open(TIMESTAMP_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ JSON 파싱 오류: {e}")
        return []


def load_vod_config() -> list[dict]:
    """data/vod_config.json에서 VOD 설정을 로드합니다."""
    if not VOD_CONFIG_FILE.exists():
        return []
    
    try:
        with open(VOD_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ VOD 설정 파싱 오류: {e}")
        return []


def save_vod_config(vod_segments: list[dict]) -> None:
    """VOD 설정을 저장합니다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(VOD_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(vod_segments, f, ensure_ascii=False, indent=2)
    print(f"✅ VOD 설정 저장 완료: {VOD_CONFIG_FILE}")


def create_vod_segments(vod_config: list[dict]) -> list[VodSegment]:
    """VOD 설정에서 VodSegment 객체 리스트를 생성합니다."""
    segments = []
    
    for i, config in enumerate(vod_config):
        start_time = parse_datetime(config["start_time"])
        
        # 다음 세그먼트의 시작 시간이 이 세그먼트의 종료 시간
        end_time = None
        if i + 1 < len(vod_config):
            end_time = parse_datetime(vod_config[i + 1]["start_time"])
        
        segments.append(VodSegment(
            url=config["url"],
            start_time=start_time,
            end_time=end_time
        ))
    
    return segments


def find_matching_segment(meme_time_kst: datetime, segments: list[VodSegment]) -> Optional[VodSegment]:
    """밈 시간에 해당하는 VOD 세그먼트를 찾습니다."""
    for segment in segments:
        if segment.end_time is None:
            # 마지막 세그먼트
            if meme_time_kst >= segment.start_time:
                return segment
        else:
            # 중간 세그먼트
            if segment.start_time <= meme_time_kst < segment.end_time:
                return segment
    
    return None


def generate_vod_links(meme_data: list[dict], segments: list[VodSegment]) -> list[dict]:
    """밈 데이터를 기반으로 VOD 링크 목록을 생성합니다."""
    results = []
    
    for meme in meme_data:
        # UTC → KST 변환
        meme_time_utc = parse_datetime(meme["time"])
        meme_time_kst = meme_time_utc + KST_OFFSET
        
        # 해당 VOD 세그먼트 찾기
        segment = find_matching_segment(meme_time_kst, segments)
        
        if segment is None:
            print(f"⚠️ 경고: {meme['time']} (KST: {meme_time_kst.strftime('%Y-%m-%d %H:%M:%S')})에 해당하는 VOD가 없습니다. 스킵합니다.")
            continue
        
        # 세그먼트 시작부터의 초 계산
        delta = meme_time_kst - segment.start_time
        seconds = int(delta.total_seconds())
        
        if seconds < 0:
            print(f"⚠️ 경고: 계산 오류 - {meme['time']}. 스킵합니다.")
            continue
        
        vod_link = generate_vod_link(segment.url, seconds)
        time_display = format_time_display(seconds)
        
        results.append({
            "original_time_utc": meme["time"],
            "original_time_kst": meme_time_kst.strftime("%Y-%m-%d %H:%M:%S"),
            "count": meme["count"],
            "description": meme["description"],
            "vod_url": segment.url,
            "seconds_from_start": seconds,
            "time_display": time_display,
            "vod_link": vod_link
        })
    
    # 시간순 정렬
    results.sort(key=lambda x: x["original_time_kst"])
    
    return results


def print_results(results: list[dict], segments: list[VodSegment]) -> None:
    """결과를 보기 좋게 출력합니다."""
    print("\n" + "=" * 80)
    print("🎬 VOD 타임스탬프 링크 생성 결과")
    print("=" * 80)
    
    print("\n📺 VOD 세그먼트:")
    for i, seg in enumerate(segments, 1):
        end_str = seg.end_time.strftime("%H:%M:%S") if seg.end_time else "끝"
        print(f"   [{i}] {seg.start_time.strftime('%H:%M:%S')} ~ {end_str}")
        print(f"       {seg.url}")
    
    print("\n" + "=" * 80 + "\n")
    
    for i, result in enumerate(results, 1):
        print(f"📌 [{i}] {result['description']}")
        print(f"   🕐 발생 시각: {result['original_time_kst']} (KST)")
        print(f"   ⏱️  타임스탬프: {result['time_display']}")
        print(f"   🔗 {result['vod_link']}")
        print()
    
    print("=" * 80)
    print(f"✅ 총 {len(results)}개의 VOD 링크 생성 완료")
    print("=" * 80 + "\n")
    
    # 링크만 간단히 출력
    print("📋 링크 목록 (복사용):")
    print("-" * 60)
    for result in results:
        print(f"[{result['time_display']}] {result['vod_link']}")
    print("-" * 60 + "\n")


def input_vod_segments() -> list[dict]:
    """사용자로부터 VOD 세그먼트 정보를 입력받습니다."""
    print("\n📺 VOD 세그먼트 입력")
    print("-" * 40)
    print("여러 VOD가 있으면 시간순으로 입력하세요.")
    print("입력 완료 시 빈 URL을 입력하세요.\n")
    
    segments = []
    index = 1
    
    while True:
        print(f"[VOD {index}]")
        url = input("  URL (완료시 Enter): ").strip()
        
        if not url:
            break
        
        start_time = input("  방송 시작 시간 (YYYY-MM-DD HH:MM:SS): ").strip()
        
        try:
            parse_datetime(start_time)
        except ValueError:
            print("  ❌ 잘못된 시간 형식입니다. 다시 입력하세요.")
            continue
        
        segments.append({
            "url": url,
            "start_time": start_time
        })
        
        print(f"  ✅ VOD {index} 추가 완료\n")
        index += 1
    
    return segments


def main():
    """메인 함수"""
    print("\n🎬 VOD 타임스탬프 링크 생성기 (다중 VOD 지원)")
    print("=" * 50)
    
    # 밈 데이터 로드
    meme_data = load_timestamp_data()
    if not meme_data:
        print("❌ 밈 데이터가 없습니다. data/timestamp.json 파일을 확인하세요.")
        return
    
    print(f"✅ {len(meme_data)}개의 밈 데이터 로드 완료")
    
    # VOD 설정 로드 또는 입력
    vod_config = load_vod_config()
    
    if vod_config:
        print(f"✅ 기존 VOD 설정 로드 완료 ({len(vod_config)}개 세그먼트)")
        use_existing = input("\n기존 설정을 사용하시겠습니까? (Y/n): ").strip().lower()
        if use_existing == 'n':
            vod_config = input_vod_segments()
            if vod_config:
                save_vod_config(vod_config)
    else:
        print("📝 VOD 설정이 없습니다. 새로 입력해주세요.")
        vod_config = input_vod_segments()
        if vod_config:
            save_vod_config(vod_config)
    
    if not vod_config:
        print("❌ VOD 설정이 없습니다.")
        return
    
    # VOD 세그먼트 생성
    segments = create_vod_segments(vod_config)
    
    # VOD 링크 생성
    results = generate_vod_links(meme_data, segments)
    
    if not results:
        print("❌ 생성된 링크가 없습니다.")
        return
    
    # 결과 출력
    print_results(results, segments)


if __name__ == "__main__":
    main()
