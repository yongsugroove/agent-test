# database.py
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv
from pydantic_settings import BaseSettings 

load_dotenv()
class DBSettings(BaseSettings):
    HOSTNAME: str
    PORT: int
    PASSWORD: str
    NAME: str
    USERNAME: str

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        env_prefix = 'DATABASE_'  # 'DB_'로 시작하는 환경 변수만 읽도록 설정
        extra = 'ignore'    # 정의되지 않은 추가 필드가 들어오면 오류 발생 (기존 유지 또는 변경)

settings = DBSettings()

SQLALCHEMY_DATABASE_URL = (
    f"mysql+asyncmy://{settings.USERNAME}:{settings.PASSWORD}@"
    f"{settings.HOSTNAME}:{settings.PORT}/{settings.NAME}"
    "?charset=utf8mb4" # 유니코드 및 문자 인코딩 설정
)

# --- SQLAlchemy 비동기 엔진 생성 ---
# Spring Boot HikariCP 설정 매핑:
engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_size=20,           
    pool_recycle=18000,        # 유휴 연결 재생성 주기 (초). 더 길게(예: 1800) 설정할 수도 있음.
    pool_timeout=300,        # 풀에서 연결을 가져올 때 대기 시간 (초).
    pool_pre_ping=True,
    echo=True      
)

# --- 비동기 세션 생성기 ---
AsyncSessionLocal = sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

# --- ORM 모델을 위한 기본 클래스 ---
Base = declarative_base()

# --- FastAPI 의존성 주입 함수 ---
async def get_db() -> AsyncSession:
    """각 요청에 대한 DB 세션을 생성하고 완료 후 닫는 의존성 (단순화 버전)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        finally:
            await session.close()