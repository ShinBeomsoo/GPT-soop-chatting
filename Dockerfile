# 가벼운 Python 이미지 사용
FROM python:3.10-slim

# 작업 디렉토리 설정
WORKDIR /app

# 의존성 파일 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY soop_chat_ji.py .

# 포트 노출 (Fly.io 기본 포트 8080 권장)
EXPOSE 8080

# 앱 실행 (host 0.0.0.0 필수)
CMD ["uvicorn", "soop_chat_ji:app", "--host", "0.0.0.0", "--port", "8080"]
