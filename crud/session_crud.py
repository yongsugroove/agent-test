import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# models.py에서 SessionInfo 모델 임포트 시도
try:
    from models import SessionInfo
except ImportError:
    import sys
    import os
    # 프로젝트 루트를 기준으로 경로 추가 (조정 필요할 수 있음)
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from models import SessionInfo

logger = logging.getLogger("crud.session")

async def get_session_by_id(db: AsyncSession, session_id: str) -> Optional[SessionInfo]:
    """주어진 세션 ID로 세션 정보를 조회합니다."""
    result = await db.execute(
        select(SessionInfo).where(SessionInfo.session_id == session_id)
    )
    return result.scalars().first()

async def create_session(db: AsyncSession, session_id: str, user_id: str, company_code: int, session_title: Optional[str] = None) -> SessionInfo:
    """새로운 세션 정보를 생성합니다."""
    new_session = SessionInfo(
        session_id=session_id,
        user_id=user_id,
        company_code=company_code,
        session_title=session_title
        # reg_date는 모델의 default 값(datetime.utcnow) 사용
    )
    db.add(new_session)
    # commit은 호출하는 쪽(API 핸들러)에서 수행
    # ID 등 자동 생성 값을 즉시 얻으려면 flush 필요
    await db.flush()
    await db.refresh(new_session) # DB에서 최신 상태 로드 (선택 사항)
    logger.info(f"New session created: {session_id} for user {user_id}, title: {session_title}")
    return new_session

async def get_or_create_session(db: AsyncSession, session_id: str, user_id: str, company_code: int, session_title: Optional[str] = None) -> SessionInfo:
    """
    주어진 세션 ID로 세션 정보를 조회하고, 없으면 새로 생성합니다.
    만약 세션이 존재하지만 user_id 또는 company_code가 다른 경우,
    현재 구현에서는 기존 세션 정보를 반환합니다. (정책에 따라 수정 가능)
    """
    existing_session = await get_session_by_id(db, session_id)
    
    if existing_session:
        logger.debug(f"Existing session found: {session_id}")
        # 필요시 여기서 user_id, company_code 일치 여부 확인 또는 업데이트 로직 추가
        # 예: if existing_session.user_id != user_id or existing_session.company_code != company_code:
        #        logger.warning(...)
        #        # 정책 결정: 업데이트? 오류?
        #        existing_session.user_id = user_id # 예시: 업데이트
        #        existing_session.company_code = company_code
        #        existing_session.reg_date = datetime.utcnow() # 업데이트 시간 갱신
        return existing_session
    else:
        logger.debug(f"No existing session found for {session_id}. Creating new session.")
        return await create_session(db, session_id, user_id, company_code, session_title)

async def update_session_title(db: AsyncSession, session_id: str, new_title: str) -> Optional[SessionInfo]:
    """
    세션의 제목을 업데이트합니다.
    세션이 존재하지 않으면 None을 반환합니다.
    """
    session = await get_session_by_id(db, session_id)
    if not session:
        logger.warning(f"Cannot update title: Session not found for ID: {session_id}")
        return None
    
    session.session_title = new_title
    await db.flush()
    logger.info(f"Session title updated: {session_id} -> '{new_title}'")
    return session

async def get_all_sessions_by_user(db: AsyncSession, user_id: str, company_code: Optional[int] = None) -> list[SessionInfo]:
    """
    특정 사용자의 모든 세션 정보를 조회합니다.
    company_code가 제공되면 해당 회사 코드로 필터링합니다.
    """
    query = select(SessionInfo).where(SessionInfo.user_id == user_id)
    
    if company_code is not None:
        query = query.where(SessionInfo.company_code == company_code)
    
    # 최신 세션이 먼저 나오도록 정렬
    query = query.order_by(SessionInfo.reg_date.desc())
    
    result = await db.execute(query)
    return result.scalars().all()

# 필요에 따라 다른 CRUD 함수 추가 가능 (예: update_session_activity, delete_session 등) 