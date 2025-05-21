from typing import List, Optional, Dict
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

# models.py에서 PromptInfo 모델 임포트
# 경로가 맞는지 확인 필요 (예: from .. import models)
try:
    from models import PromptInfo 
except ImportError:
    # 다른 경로 시도 (예: 프로젝트 루트 기준)
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from models import PromptInfo


# 시스템 전체 기본 프롬프트 식별자 (설정 가능)
DEFAULT_USER_ID = "default-prompt"
DEFAULT_COMPANY_CODE = 3# 또는 시스템 기본값을 나타내는 다른 코드

async def get_all_prompts_formatted(db: AsyncSession, user_id: str, company_code: int) -> Dict[str, Dict[str, str]]:
    """
    특정 사용자 및 회사의 모든 프롬프트와 시스템 기본 프롬프트를 가져와 
    기존 JSON과 유사한 default/custom 구조로 반환합니다.
    """
    # 사용자 및 회사 특정 프롬프트 조회 (커스텀 프롬프트)
    user_result = await db.execute(
        select(PromptInfo)
        .where(PromptInfo.user_id == user_id, PromptInfo.company_code == company_code)
    )
    user_prompts = user_result.scalars().all()

    # 시스템 기본 프롬프트 조회
    default_result = await db.execute(
        select(PromptInfo)
        .where(PromptInfo.user_id == DEFAULT_USER_ID, PromptInfo.company_code == DEFAULT_COMPANY_CODE)
    )
    default_prompts = default_result.scalars().all()

    # 결과 포맷팅
    formatted_prompts = {"default": {}, "custom": {}}

    # 기본 프롬프트 처리 (prompt_type을 키로 사용)
    for p in default_prompts:
        # prompt_type에서 접두사(예: 'default_') 제거 고려 -> 여기서는 그대로 사용
        formatted_prompts["default"][p.prompt_type] = p.prompt_text

    # 사용자 커스텀 프롬프트 처리 (prompt_type을 키로 사용)
    for p in user_prompts:
        # prompt_type에서 접두사(예: 'custom_') 제거 고려 -> 여기서는 그대로 사용
        formatted_prompts["custom"][p.prompt_type] = p.prompt_text

    return formatted_prompts

async def get_effective_prompt(db: AsyncSession, user_id: str, company_code: int, prompt_type: str) -> Optional[str]:
    """
    특정 타입의 유효한 프롬프트를 가져옵니다 (사용자/회사 커스텀 우선, 없으면 시스템 기본).
    prompt_type은 function_name 또는 기본 타입을 그대로 사용합니다.
    """
    # 1. 사용자/회사 커스텀 프롬프트 조회
    row = await db.execute(
        select(PromptInfo.prompt_text)
        .where(PromptInfo.user_id == user_id, 
               PromptInfo.company_code == company_code, 
               PromptInfo.prompt_type == prompt_type) # 타입 이름 그대로 조회
    )
    prompt_custom = row.scalar_one_or_none()
    if prompt_custom is not None:
        return prompt_custom

    # 2. 시스템 기본 프롬프트 조회
    row_default = await db.execute(
        select(PromptInfo.prompt_text)
        .where(PromptInfo.user_id == DEFAULT_USER_ID, 
               PromptInfo.company_code == DEFAULT_COMPANY_CODE, 
               PromptInfo.prompt_type == prompt_type) # 타입 이름 그대로 조회
    )
    prompt_default = row_default.scalar_one_or_none()
    if prompt_default is not None:
        return prompt_default
        
    return None # 해당하는 프롬프트 없음

async def save_prompt(db: AsyncSession, user_id: str, company_code: int, prompt_type: str, prompt_text: str) -> PromptInfo:
    """
    특정 사용자/회사의 특정 타입 프롬프트를 저장하거나 업데이트합니다.
    """
    # 기존 프롬프트 조회
    result = await db.execute(
        select(PromptInfo)
        .where(PromptInfo.user_id == user_id, 
               PromptInfo.company_code == company_code, 
               PromptInfo.prompt_type == prompt_type)
    )
    db_prompt = result.scalars().first()

    if db_prompt:
        # 업데이트
        if db_prompt.prompt_text != prompt_text:
            db_prompt.prompt_text = prompt_text
            db_prompt.reg_date = datetime.utcnow() # 수정일 업데이트
            await db.commit()
            await db.refresh(db_prompt)
        return db_prompt
    else:
        # 새로 생성
        new_prompt = PromptInfo(
            user_id=user_id, 
            company_code=company_code, 
            prompt_type=prompt_type, 
            prompt_text=prompt_text
            # reg_date는 default 값 사용 (utcnow)
        )
        db.add(new_prompt)
        await db.commit()
        await db.refresh(new_prompt)
        return new_prompt

async def delete_prompt(db: AsyncSession, user_id: str, company_code: int, prompt_type: str) -> bool:
    """특정 사용자/회사의 특정 타입 프롬프트를 삭제합니다."""
    result = await db.execute(
        delete(PromptInfo)
        .where(PromptInfo.user_id == user_id, 
               PromptInfo.company_code == company_code, 
               PromptInfo.prompt_type == prompt_type)
    )
    await db.commit()
    return result.rowcount > 0 