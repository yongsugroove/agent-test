import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.mysql import insert # MySQL의 ON DUPLICATE KEY UPDATE를 위함

# models.py에서 ChatSummary 모델 임포트 시도
try:
    from models import ChatSummary
except ImportError:
    # 이 fallback은 개발 환경에 따라 조정될 수 있습니다.
    # 일반적으로는 PYTHONPATH 설정이나 가상환경 내 패키지 설치를 통해 직접 임포트가 가능해야 합니다.
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from models import ChatSummary

logger = logging.getLogger("crud.chat_summary")

async def get_summary_by_session(db: AsyncSession, session_id: str) -> Optional[ChatSummary]:
    """주어진 세션 ID에 대한 최신 채팅 요약본을 가져옵니다."""
    result = await db.execute(
        select(ChatSummary).where(ChatSummary.session_id == session_id)
    )
    summary = result.scalars().first()
    if summary:
        logger.debug(f"Summary found for session {session_id}, updated_at: {summary.updated_at}")
    else:
        logger.debug(f"No summary found for session {session_id}")
    return summary

async def create_or_update_summary(
    db: AsyncSession, 
    session_id: str, 
    summary_text: str
) -> ChatSummary:
    """
    주어진 세션 ID에 대한 채팅 요약본을 생성하거나 업데이트합니다.
    """
    # 먼저 기존 요약이 있는지 확인
    existing_summary = await get_summary_by_session(db, session_id)

    if existing_summary:
        # 업데이트
        existing_summary.summary_text = summary_text
        # updated_at은 모델의 onupdate=func.now()에 의해 자동 업데이트됨
        await db.flush()
        await db.refresh(existing_summary)
        logger.info(f"Summary updated for session {session_id}")
        return existing_summary
    else:
        # 새로 생성
        new_summary = ChatSummary(
            session_id=session_id,
            summary_text=summary_text
            # created_at, updated_at는 모델의 server_default/onupdate에 의해 자동 설정됨
        )
        db.add(new_summary)
        await db.flush()
        await db.refresh(new_summary)
        logger.info(f"New summary created for session {session_id}")
        return new_summary