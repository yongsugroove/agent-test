import os
import json
import logging
import uuid
import asyncio
from typing import Dict, List, Any, Optional
from pathlib import Path
import traceback

import httpx
from dotenv import load_dotenv

from mcp_sse_client import McpSseClient

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("async_mcp_client")

async def load_server_list() -> Dict[str, Any]:
    """서버 목록 파일에서 서버 정보를 로드합니다."""
    try:
        server_file = 'server_list.json'
        if Path(server_file).exists():
            with open(server_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                logger.info(f"서버 목록 파일 로드됨: {server_file}")
                logger.info(f"서버 목록 내용: {json.dumps(data, ensure_ascii=False, indent=2)}")
                
                # 데이터 구조 검증
                if not isinstance(data, dict) or "servers" not in data:
                    raise ValueError("잘못된 서버 목록 파일 형식")
                
                if not isinstance(data["servers"], list):
                    raise ValueError("servers 필드가 리스트가 아님")
                
                if not data["servers"]:
                    raise ValueError("서버 목록이 비어있음")
                
                # 각 서버 정보 검증
                for server in data["servers"]:
                    if not isinstance(server, dict):
                        raise ValueError(f"잘못된 서버 정보 형식: {server}")
                    if "url" not in server:
                        raise ValueError(f"서버 URL이 없음: {server}")
                
                return data
        
        logger.warning("서버 목록 파일을 찾을 수 없습니다. 기본값 사용.")
        default_data = {
            "servers": [
                {
                    "name": "local",
                    "url": "http://localhost:8000",
                    "enabled": True
                }
            ]
        }
        logger.info(f"기본 서버 목록: {json.dumps(default_data, ensure_ascii=False, indent=2)}")
        return default_data
        
    except json.JSONDecodeError as e:
        error_msg = f"서버 목록 파일 파싱 오류: {str(e)}"
        logger.error(error_msg)
        raise ValueError(error_msg)
    except Exception as e:
        error_msg = f"서버 목록 로드 중 오류: {str(e)}"
        logger.error(error_msg)
        raise

async def get_registered_tools(server_url: str) -> Dict[str, Any]:
    """서버에서 등록된 도구 목록을 가져옵니다."""
    client = None
    try:
        logger.info(f"서버 {server_url}에서 도구 목록 조회 시작")
        
        # SSE 클라이언트 생성 및 연결
        client = McpSseClient(server_url)
        connected = await client.connect()
        
        if not connected:
            logger.error(f"서버 {server_url}에 연결할 수 없습니다.")
            return {"error": "서버 연결 실패"}
            
        # 도구 목록 가져오기 (최대 10초 대기)
        tools = await asyncio.wait_for(client.get_tools(), timeout=10.0)
        logger.debug(f"받은 도구 목록: {json.dumps(tools, ensure_ascii=False, indent=2)}")
        
        if not tools:
            logger.warning(f"서버 {server_url}에서 도구를 찾을 수 없습니다.")
            return {"error": "도구를 찾을 수 없습니다."}
            
        # 도구 목록 검증 및 변환
        valid_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                logger.warning(f"잘못된 도구 형식 (딕셔너리가 아님): {tool}")
                continue
                
            if "name" not in tool:
                logger.warning(f"도구 이름이 없음: {tool}")
                continue
                
            if "description" not in tool:
                logger.warning(f"도구 설명이 없음: {tool}")
                continue
                
            if "inputSchema" not in tool:
                logger.warning(f"도구 입력 스키마가 없음: {tool}")
                continue
                
            valid_tools.append(tool)
            logger.debug(f"유효한 도구 발견: {tool['name']}")
            
        if not valid_tools:
            logger.warning(f"서버 {server_url}에서 유효한 도구를 찾을 수 없습니다.")
            return {"error": "유효한 도구를 찾을 수 없습니다."}
            
        logger.info(f"서버 {server_url}에서 {len(valid_tools)}개의 유효한 도구를 찾았습니다.")
        return {"tools": valid_tools}
            
    except asyncio.TimeoutError:
        error_msg = f"도구 목록 조회 시간 초과: {server_url}"
        logger.error(error_msg)
        return {"error": error_msg}
        
    except Exception as e:
        error_msg = f"도구 목록 조회 실패: {str(e)}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        return {"error": error_msg}
        
    finally:
        if client:
            try:
                await client.disconnect()
                logger.debug(f"서버 {server_url} 연결 종료")
            except Exception as e:
                logger.warning(f"SSE 클라이언트 종료 중 오류: {str(e)}")

async def call_mcp_tool(server_url: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    """
    MCP 서버의 도구 호출
    
    Args:
        server_url: MCP 서버 URL
        tool_name: 호출할 도구 이름
        arguments: 도구 인자
        
    Returns:
        도구 호출 결과
    """
    logger.info(f"도구 호출 시작 - 서버: {server_url}, 도구: {tool_name}")
    logger.debug(f"호출 인자: {json.dumps(arguments, ensure_ascii=False, indent=2)}")
    
    client = None
    try:
        # SSE 클라이언트 생성
        client = McpSseClient(server_url)
        
        # 서버 연결
        logger.debug(f"서버 {server_url}에 연결 시도 중...")
        connected = await client.connect()
        if not connected:
            error_msg = f"서버 연결 실패: {server_url}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        logger.info(f"서버 {server_url}에 연결 성공")
        
        # 도구 목록 확인
        logger.debug(f"도구 목록 조회 중...")
        tools = await client.get_tools()
        if not tools:
            error_msg = f"사용 가능한 도구가 없습니다: {server_url}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        logger.debug(f"사용 가능한 도구 목록: {json.dumps(tools, ensure_ascii=False, indent=2)}")
            
        # 도구 존재 여부 확인
        tool_exists = any(tool.get("name") == tool_name for tool in tools)
        if not tool_exists:
            error_msg = f"요청한 도구를 찾을 수 없습니다: {tool_name}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        # 도구 호출
        logger.debug(f"도구 {tool_name} 호출 중...")
        
        # MCP 도구 호출 시 타임아웃 값 설정 (15초)
        result = await asyncio.wait_for(
            client.invoke_tool(tool_name, arguments),
            timeout=300.0
        )
        
        if not result:
            error_msg = f"도구 호출 결과가 없음: {tool_name}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        # 오류 응답 확인
        if isinstance(result, dict) and "error" in result:
            error_msg = f"도구 호출 중 오류 발생: {result['error']}"
            logger.error(error_msg)
            return {"error": error_msg}
            
        # 대기 상태 확인
        if isinstance(result, dict) and result.get("status") == "waiting":
            logger.warning(f"도구 호출 결과 대기 중: {tool_name}")
            # 대기 상태인 경우, 클라이언트를 유지하고 결과를 기다리지 않음
            # 이 경우 클라이언트 연결 종료를 하지 않음
            client = None
            return result
        
        logger.info(f"도구 {tool_name} 호출 성공")
        logger.debug(f"호출 결과: {json.dumps(result, ensure_ascii=False, indent=2)}")
        
        return result
        
    except asyncio.TimeoutError:
        error_msg = f"도구 호출 시간 초과: {tool_name} (서버: {server_url})"
        logger.error(error_msg)
        return {"error": error_msg}
    except Exception as e:
        error_msg = f"도구 호출 중 오류 발생: {str(e)}"
        logger.error(f"{error_msg}\n{traceback.format_exc()}")
        return {"error": error_msg}
    finally:
        # 연결 종료 시도 (대기 상태가 아닌 경우에만)
        if client:
            try:
                await client.disconnect()
                logger.debug(f"서버 {server_url} 연결 종료")
            except Exception as e:
                logger.warning(f"서버 {server_url} 연결 종료 중 오류: {str(e)}")