# models.py
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, BigInteger, ForeignKey, Enum as SAEnum
from sqlalchemy.sql import func # 기본값 설정을 위해 (예: CURRENT_TIMESTAMP)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, validates # validates 추가
import enum # enum 추가
from datetime import datetime

from database import Base

class PromptInfo(Base):
    """
    프롬프트 설정 테이블 (tb_prompt_info) SQLAlchemy 모델 클래스
    """
    __tablename__ = 'tb_prompt_info'
    # 테이블 코멘트는 __table_args__ 를 통해 설정 가능 (선택 사항)
    __table_args__ = {'comment': '프롬프트 설정 테이블'} 

    # 컬럼 정의
    idx = Column(Integer, primary_key=True, autoincrement=True, comment='인덱스 (PK)')
    user_id = Column(String(200), nullable=False, comment='사용자 ID')
    prompt_type = Column(String(45), nullable=False, comment='프롬프트 타입 (예: default, custom, function_name 등)')
    prompt_text = Column(Text, nullable=True, comment='프롬프트 내용') # longtext는 Text로 매핑
    reg_date = Column(DateTime, nullable=False, default=datetime.utcnow, comment='등록(수정) 일시') # 기본값으로 현재 UTC 시간 설정 가능
    company_code = Column(Integer, nullable=False, comment='회사 코드')

    def __repr__(self):
        """객체 표현식"""
        return (f"<PromptInfo(idx={self.idx}, user_id='{self.user_id}', "
                f"prompt_type='{self.prompt_type}', company_code={self.company_code}, "
                f"reg_date='{self.reg_date}')>")


class ChatLog(Base):
    """
    채팅 로그 테이블 (tb_chat_log) SQLAlchemy 모델 클래스
    """
    __tablename__ = 'tb_chat_log'
    __table_args__ = {'comment': '채팅 로그 테이블'}

    idx = Column(BigInteger, primary_key=True, autoincrement=True, comment='인덱스 (PK)') # bigint -> BigInteger
    session_id = Column(String(36), nullable=False, comment='세션 ID (UUID)')
    user_id = Column(String(200), nullable=False, comment='사용자 ID')
    type = Column(String(45), nullable=True, comment='메시지 타입 (예: user, assistant, system)') # 'type'은 예약어일 수 있으므로 주의
    message = Column(Text, nullable=True, comment='메시지 내용') # mediumtext는 Text로 매핑 (충분히 길다면)
    reg_date = Column(DateTime, nullable=False, default=datetime.utcnow, comment='등록 일시')
    task = Column(String(45), nullable=True, comment='관련 작업 (선택 사항)')
    company_code = Column(Integer, nullable=False, comment='회사 코드')

    def __repr__(self):
        return (f"<ChatLog(idx={self.idx}, session_id='{self.session_id}', user_id='{self.user_id}', "
                f"type='{self.type}', reg_date='{self.reg_date}', company_code={self.company_code})>")


class SummarizeChatLog(Base):
    """
    채팅 요약 테이블 (tb_summarize_chat_log) SQLAlchemy 모델 클래스
    """
    __tablename__ = 'tb_summarize_chat_log'
    __table_args__ = {'comment': '채팅 요약 테이블'}

    idx = Column(BigInteger, primary_key=True, autoincrement=True, comment='인덱스 (PK)')
    user_id = Column(String(200), nullable=False, comment='사용자 ID')
    summary_text = Column(Text, nullable=True, comment='요약된 내용') # mediumtext -> Text
    reg_date = Column(DateTime, nullable=False, default=datetime.utcnow, comment='등록 일시')

    def __repr__(self):
        return (f"<SummarizeChatLog(idx={self.idx}, user_id='{self.user_id}', reg_date='{self.reg_date}')>")


class SessionInfo(Base):
    """
    세션 정보 테이블 (tb_session_info) SQLAlchemy 모델 클래스
    """
    __tablename__ = 'tb_session_info'
    __table_args__ = {'comment': '세션 정보 테이블'}

    idx = Column(BigInteger, primary_key=True, autoincrement=True, comment='인덱스 (PK)')
    session_id = Column(String(36), nullable=False, unique=True, comment='세션 ID (UUID, 고유)') # unique=True 추가 고려
    user_id = Column(String(200), nullable=False, comment='사용자 ID')
    company_code = Column(Integer, nullable=False, comment='회사 코드')
    reg_date = Column(DateTime, nullable=False, default=datetime.utcnow, comment='세션 시작(등록) 일시')
    session_title = Column(String(255), nullable=True, comment='세션 제목')

    def __repr__(self):
        return (f"<SessionInfo(idx={self.idx}, session_id='{self.session_id}', user_id='{self.user_id}', "
                f"company_code={self.company_code}, reg_date='{self.reg_date}', session_title='{self.session_title}')>")

# 새로운 ChatSummary 모델 정의
class ChatSummary(Base):
    """
    채팅 요약 테이블 (tb_chat_summary) SQLAlchemy 모델 클래스
    """
    __tablename__ = 'tb_chat_summary'

    idx = Column(BigInteger, primary_key=True, index=True, autoincrement=True)
    session_id = Column(String(255), index=True, unique=True, nullable=False) # 세션 ID, 각 세션당 하나의 요약만 가정
    summary_text = Column(Text, nullable=False) # 요약된 텍스트
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<ChatSummary(idx={self.idx}, session_id='{self.session_id}', updated_at='{self.updated_at}')>"
