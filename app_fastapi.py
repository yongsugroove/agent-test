import os
import logging
import json
import uuid
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional, Union, Set
from datetime import datetime
import traceback
import requests
import console_service

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse, RedirectResponse, PlainTextResponse
from pydantic import BaseModel
from dotenv import load_dotenv
from sse_starlette.sse import EventSourceResponse

from async_llm_mcp_client import AsyncLLMMcpClient, SseLogHandler
from mcp_sse_client import McpSseClient
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db, AsyncSessionLocal # AsyncSessionLocal 직접 임포트 (startup에서 사용 위함)
from crud.prompt_crud import DEFAULT_USER_ID, DEFAULT_COMPANY_CODE # 기본 ID/코드 임포트
# crud 함수 임포트 (경로 주의)
try:
    from crud import prompt_crud, session_crud, chat_log_crud, chat_summary_crud # chat_summary_crud 임포트 추가
except ImportError:
    import sys
    import os
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from crud import prompt_crud, session_crud, chat_log_crud, chat_summary_crud # chat_summary_crud 임포트 추가

# --- Start: Added for SSE Log Streaming ---
log_queue = asyncio.Queue() # 로그 큐
llm_client: AsyncLLMMcpClient = None # Placeholder for the initialized client
# --- End: Added for SSE Log Streaming ---

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("mcp_app")

# FastAPI 앱 생성
app = FastAPI(title="MCP 클라이언트", root_path="/mcp-agent")
#app = FastAPI(title="MCP 클라이언트")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 템플릿 설정
templates = Jinja2Templates(directory="templates")

# 정적 파일 설정
app.mount("/static", StaticFiles(directory="static"), name="static")

# 서버 목록 파일 경로 설정
SERVER_LIST_FILE = 'server_list.json'
PROMPTS_FILE = 'prompts.json'

# 서버 목록 및 클라이언트 캐시
server_clients = {}


# 서버 목록 로드 함수
async def load_server_list():
    """서버 목록 파일에서 서버 정보를 로드합니다."""
    try:
        if Path(SERVER_LIST_FILE).exists():
            logger.info(f"서버 목록 파일 로드 시작: {SERVER_LIST_FILE}")
            with open(SERVER_LIST_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if "servers" not in data:
                    logger.warning("서버 목록에 'servers' 키가 없습니다. 빈 목록 생성")
                    data = {"servers": []}
                else:
                    logger.info(f"서버 목록 로드 완료: {len(data['servers'])}개 서버 발견")
                    for server in data['servers']:
                        logger.info(f"서버 정보: 이름={server.get('name', 'N/A')}, URL={server.get('url', 'N/A')}, 활성화={server.get('enabled', True)}")
                return data
        logger.warning(f"서버 목록 파일이 없습니다: {SERVER_LIST_FILE}")
        return {"servers": []}
    except Exception as e:
        logger.error(f"서버 목록 로드 중 오류: {str(e)}\n{traceback.format_exc()}")
        return {"servers": []}

# 서버 목록 저장 함수
async def save_server_list(data):
    """서버 목록 정보를 파일에 저장합니다."""
    try:
        # data에 servers 배열만 포함하도록 정리
        clean_data = {"servers": data.get("servers", [])}
        
        with open(SERVER_LIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(clean_data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"서버 목록 저장 중 오류: {str(e)}")
        return False

# 활성화된 서버 URL 맵 반환
async def get_server_map():
    """활성화된 서버의 이름-URL 맵을 반환합니다."""
    logger.info("서버 맵 생성 시작")
    server_data = await load_server_list()
    servers_map = {}
    
    for server in server_data.get("servers", []):
        name = server.get("name")
        url = server.get("url")
        enabled = server.get("enabled", True)
        
        logger.info(f"서버 처리 중: 이름={name}, URL={url}, 활성화={enabled}")
        
        if not name or not url:
            logger.warning(f"서버 정보 누락: 이름={name}, URL={url}")
            continue
            
        if not url.startswith(('http://', 'https://')):
            logger.error(f"잘못된 URL 형식: {url} (서버: {name})")
            continue
            
        if enabled:
            logger.info(f"활성화된 서버 맵에 추가: {name} -> {url}")
            servers_map[name] = url
        else:
            logger.info(f"비활성화된 서버 제외: {name}")
    
    logger.info(f"서버 맵 생성 완료. 총 {len(servers_map)}개 서버")
    for name, url in servers_map.items():
        logger.info(f"최종 서버 맵 항목: {name} -> {url}")
    
    return servers_map

# 서버 MCP 클라이언트 가져오기
async def get_or_create_mcp_client(server_name: str):
    """서버 이름으로 MCP 클라이언트를 가져오거나 생성합니다."""
    global server_clients
    logger.info(f"MCP 클라이언트 요청: 서버 이름={server_name}")
    
    server_map = await get_server_map()
    logger.info(f"서버 맵 조회 결과: {json.dumps(server_map, ensure_ascii=False)}")
    
    if server_name not in server_map:
        logger.error(f"서버를 찾을 수 없음: {server_name}")
        raise HTTPException(status_code=404, detail=f"서버를 찾을 수 없습니다: {server_name}")
    
    server_url = server_map[server_name]
    logger.info(f"서버 URL 확인: 이름={server_name}, URL={server_url}")
    
    if not server_url.startswith(('http://', 'https://')):
        logger.error(f"잘못된 URL 형식: {server_url}")
        raise HTTPException(status_code=500, detail=f"잘못된 URL 형식: {server_url}")
    
    # 클라이언트가 캐시에 없거나 초기화가 안된 경우 새로 생성
    if server_name not in server_clients or not server_clients[server_name].initialized:
        logger.info(f"새 클라이언트 생성: 서버={server_name}, URL={server_url}")
        client = McpSseClient(server_url)
        connected = await client.connect()
        
        if not connected:
            logger.error(f"서버 연결 실패: {server_name} (URL: {server_url})")
            raise HTTPException(status_code=500, detail=f"서버 연결 실패: {server_name}")
        
        logger.info(f"클라이언트 생성 및 연결 성공: {server_name}")
        server_clients[server_name] = client
    else:
        logger.info(f"캐시된 클라이언트 사용: {server_name}")
    
    return server_clients[server_name]

# LLM MCP 클라이언트 생성 (초기화는 startup 이벤트에서 수행)
llm_client: Optional[AsyncLLMMcpClient] = None

# 의존성 - LLM 클라이언트 제공
async def get_llm_client():
    # llm_client가 None인 경우, 즉시 초기화 시도 또는 에러 발생
    # 이 부분은 애플리케이션의 생명주기 관리 전략에 따라 달라질 수 있습니다.
    # 현재는 startup_event에서 초기화되므로, 정상적이라면 None이 아니어야 합니다.
    if llm_client is None:
        # fallback: 만약 startup에서 실패했거나 순서가 꼬였을 경우를 대비 (권장되지는 않음)
        # logger.warning("LLM 클라이언트가 None입니다. Startup에서 초기화되지 않았거나 문제가 발생했습니다. 즉시 초기화를 시도합니다.")
        # temp_db_session_for_fallback = AsyncSessionLocal()
        # try:
        #     # global llm_client # 전역 변수 수정 명시
        #     # llm_client = await AsyncLLMMcpClient().initialize() # initialize는 이제 인자 없음
        #     # logger.info("LLM 클라이언트 비상 초기화 완료.")
        # except Exception as e_init:
        #     logger.error(f"LLM 클라이언트 비상 초기화 실패: {e_init}")
        #     raise HTTPException(status_code=503, detail="LLM 서비스가 현재 사용 불가능합니다 (초기화 실패).")
        pass # 현재는 startup에서 반드시 초기화된다고 가정하고, None이면 그대로 반환 (오류는 호출부에서 처리)
    return llm_client

# 모델 정의
class ChatMessage(BaseModel):
    message: str
    user_id: Optional[str] = None
    user_company: Optional[str] = None
    session_id: str
    image_data: Optional[str] = None # 이미지 데이터 (Base64 Data URL) 필드 추가

class ServerInfo(BaseModel):
    name: str
    url: str
    enabled: bool = True

class ToggleServer(BaseModel):
    name: str
    enabled: bool

class DeleteServer(BaseModel):
    name: str

class ToolRequest(BaseModel):
    tool_name: str
    inputs: Dict[str, Any]

# 프롬프트 관리 모델
class PromptRequest(BaseModel):
    prompt: str

class SummarizeRequest(BaseModel):
    history: List[Dict[str, Any]]

# LLM 컨텍스트 구성을 위한 상수값 (조정 가능)
MAX_RECENT_LOGS_FOR_CONTEXT = 10  # 요약본과 함께 LLM에 전달될 최근 로그의 최대 개수
SUMMARY_TRIGGER_LOG_COUNT = 20    # 이 개수 이상의 로그가 쌓이면 요약 시도 (요약본 없을 시 기준), 이전 요약 이후 이만큼 새 로그가 쌓이면 추가 요약

# 애플리케이션 시작 및 종료 이벤트 핸들러  
@app.on_event("startup")
async def startup_event():
    """서버 시작 시 초기화 작업을 수행합니다."""
    try:
        logger.info("서버 초기화 시작")
        
        global llm_client
        # llm_client.initialize()는 이제 db 세션이나 사용자 컨텍스트 인자를 받지 않음
        llm_client = await AsyncLLMMcpClient().initialize()
        
        if hasattr(llm_client, 'set_logging_callback'):
            async def sse_log_pusher(log_type: str, log_data: dict):
                await log_queue.put(log_data)
            llm_client.set_logging_callback(sse_log_pusher)
            logger.info("LLM 로깅 콜백 설정 완료 (큐 사용)")

            # SseLogHandler를 FastAPI 애플리케이션 로거에 추가
            # llm_client가 성공적으로 초기화되고 콜백이 설정된 후에 핸들러 추가
            sse_handler_for_fastapi_logger = SseLogHandler(client_instance=llm_client)
            # app_fastapi.py에서 사용하는 logger 인스턴스 (전역 logger 변수)에 핸들러 추가
            logger.addHandler(sse_handler_for_fastapi_logger)
            logger.info("SseLogHandler를 FastAPI 애플리케이션 로거 (mcp_app)에 추가했습니다.")
        else:
            logger.warning("LLM 클라이언트에 로깅 콜백 기능이 없습니다.")
        
        logger.info("서버 초기화 완료")
    except Exception as e:
        logger.error(f"서버 초기화 실패: {str(e)}", exc_info=True) # exc_info 추가
        # raise # 여기서 raise하면 서버 시작이 안될 수 있으므로, 로깅 후 정상 진행 또는 다른 처리 고려

@app.on_event("shutdown")
async def shutdown_event():
    """애플리케이션 종료 시 정리 작업 수행"""
    logger.info("애플리케이션 종료")

# 대화 기록 저장
chat_history = []

# WebSocket 연결 관리를 위한 클래스
class LogManager:
    def __init__(self):
        self.connections: Set[WebSocket] = set()
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.add(websocket)
        
    def disconnect(self, websocket: WebSocket):
        self.connections.remove(websocket)
        
    async def broadcast(self, message: str):
        if not self.connections:
            return
            
        disconnected = set()
        for connection in self.connections:
            try:
                await connection.send_text(message)
            except:
                disconnected.add(connection)
                
        for connection in disconnected:
            self.connections.remove(connection)

# 로그 매니저 인스턴스 생성
log_manager = LogManager()

# WebSocket 로그 핸들러 클래스
class WebSocketLogHandler(logging.Handler):
    def __init__(self, log_manager: LogManager):
        super().__init__()
        self.log_manager = log_manager
        
    def emit(self, record):
        try:
            log_entry = {
                'timestamp': record.created,
                'level': record.levelname,
                'message': record.getMessage(),
                'module': record.module,
                'function': record.funcName,
                'line': record.lineno
            }
            asyncio.create_task(self.log_manager.broadcast(json.dumps(log_entry)))
        except Exception as e:
            logger.error(f"로그 전송 중 오류 발생: {str(e)}")

# 로그 핸들러 설정
ws_handler = WebSocketLogHandler(log_manager)
ws_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
# logger.addHandler(ws_handler) # 핸들러 추가 로직 제거

# WebSocket 엔드포인트
@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await log_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket 연결 중 오류 발생: {str(e)}")
        log_manager.disconnect(websocket)

# WebSocket 엔드포인트
@app.websocket("/ws/debug")
async def websocket_debug_endpoint(websocket: WebSocket):
    await log_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WebSocket 디버그 연결 중 오류 발생: {str(e)}")
        log_manager.disconnect(websocket)

# 라우트 정의
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    """웹 인터페이스 메인 페이지 및 세션 처리"""
    user_ip = request.client.host
    user_agent = request.headers.get("user-agent", "Unknown")
    
    # 1. 쿼리 파라미터에서 sessionId 읽기
    session_id_from_query = request.query_params.get("sessionId")
    logger.info(f"[세션] Query param sessionId: {session_id_from_query}")
    
    # 사용자 인증 및 정보 가져오기 (기존 로직과 유사하게 진행)
    access_token = request.cookies.get("access_token")
    user_id = request.cookies.get("user_id")
    retrieved_user_info = None
    error_message_for_template = None
    login_required_for_template = False
    auth_url = None # auth_url 초기화
    company_code = 0 # company_code 초기화 (기본값 또는 인증 전 값)

    # 임시 사용자 컨텍스트 (인증 완료 후 실제 값으로 채워짐)
    # UserContext 모델이 정의되어 있다고 가정
    # get_current_user_context를 여기서 직접 호출하기보다, 인증 로직 후 user_id, company_code 확정
    
    logger.info(f"[사용자 접속] IP: {user_ip}, UserAgent: {user_agent}, Initial UserID from cookie: {user_id}")

    try:
        if console_service.cookie_check(request_page=request):
            logger.info("[인증 상태] 유효한 토큰 발견. 사용자 정보 조회 시도.")
            try:
                retrieved_user_info = console_service.get_user_info(access_token)
                if retrieved_user_info and isinstance(retrieved_user_info, dict):
                    logger.info(f"[사용자 전체정보] {json.dumps(retrieved_user_info, ensure_ascii=False, indent=2)}")
                    current_user_id_from_info = str(retrieved_user_info.get('recv', {}).get('id', ''))
                    current_company_code = retrieved_user_info.get('recv', {}).get('company') # 'company_code' 대신 'company' 사용 (이전 수정 반영)
                    
                    if current_user_id_from_info:
                        user_id = current_user_id_from_info # user_id 업데이트
                    if current_company_code is not None:
                        try:
                            company_code = int(current_company_code)
                        except ValueError:
                            logger.warning(f"[인증 상태] company_code ('{current_company_code}')가 정수가 아닙니다. 기본값을 사용합니다.")
                            company_code = 0 # 또는 적절한 오류 처리
                    else:
                        logger.warning("[인증 상태] 사용자 정보에 company_code('company')가 없습니다.")
                        company_code = 0 # 기본값 사용

                else:
                    logger.warning("[사용자 정보] 유효한 사용자 정보를 가져오지 못했습니다. 토큰 만료 또는 오류 가능성.")
                    access_token = None
                    user_id = None
                    retrieved_user_info = None
                    company_code = 0
            except Exception as e:
                logger.error(f"[오류] 유효 토큰으로 사용자 정보 조회 중 오류: {str(e)}")
                retrieved_user_info = None
                access_token = None
                user_id = None
                company_code = 0
        
        else:
            logger.info("[인증 상태] 토큰 없거나 무효. 인증 URL 생성 및 코드 확인 시도.")
            url_prefix = request.headers.get("x-forwarded-prefix", "/mcp-agent").rstrip('/') # root_path 고려
            auth_url = console_service.get_aceess_code(url_prefix)
            logger.info(f"[인증 URL] {auth_url}")
                
            access_code_from_query = request.query_params.get("code")
            logger.info(f"[Request Query Params] {request.query_params}")

            if access_code_from_query is not None:
                logger.info(f"Access Code 발견: {access_code_from_query}")
                token_data = console_service.get_access_token(access_code_from_query, url_prefix)
                if token_data and token_data.get('access_token'):
                    access_token = token_data['access_token']
                    logger.info(f"새 Access Token 발급 성공: {access_token[:10]}...")
                    retrieved_user_info = console_service.get_user_info(access_token)
                    if retrieved_user_info and isinstance(retrieved_user_info, dict):
                        logger.info(f"[사용자 정보] 새 토큰으로 정보 조회 성공.")
                        new_user_id = str(retrieved_user_info.get('recv', {}).get('id', ''))
                        new_company_code = retrieved_user_info.get('recv', {}).get('company')
                        if new_user_id:
                            user_id = new_user_id
                        if new_company_code is not None:
                            try: 
                                company_code = int(new_company_code)
                            except ValueError:
                                logger.warning(f"[인증 상태] 새 토큰: company_code ('{new_company_code}')가 정수가 아닙니다.")
                                company_code = 0
                        else: 
                            company_code = 0
                    else:
                        logger.warning("[사용자 정보] 새 토큰으로 사용자 정보를 가져오지 못했습니다.")
                        retrieved_user_info = None; access_token = None; user_id = None; company_code = 0
                        error_message_for_template = "사용자 정보를 가져오는데 실패했습니다."
                else:
                    logger.error("[오류] Access Token 발급 실패 또는 데이터 유효하지 않음.")
                    access_token = None; user_id = None; retrieved_user_info = None; company_code = 0
                    error_message_for_template = "로그인에 실패했습니다. 다시 시도해주세요."
            else:
                logger.info("URL에 'code' 파라미터 없음. 로그인 필요.")
                login_required_for_template = True
                # auth_url은 이미 위에서 생성됨

        # --- 세션 정보 처리 (user_id가 확정된 후에 수행) ---
        if user_id and session_id_from_query:
            try:
                # 세션 제목이 쿼리 파라미터로 제공되었는지 확인
                session_title_from_query = request.query_params.get("sessionTitle")
                session_info = await session_crud.get_or_create_session(
                    db=db, 
                    session_id=session_id_from_query, 
                    user_id=user_id, 
                    company_code=company_code,
                    session_title=session_title_from_query
                )
                await db.commit() # 세션 정보 생성/조회 후 커밋
                logger.info(f"[세션] 정보 처리 완료. Session ID: {session_info.session_id}, User ID: {session_info.user_id}, Title: {session_info.session_title}")
            except Exception as e_session:
                await db.rollback() # 세션 처리 중 오류 발생 시 롤백
                logger.error(f"[세션] 정보 처리 중 오류 발생: {e_session}", exc_info=True)
                # 세션 처리 오류가 사용자에게 필수적인 오류인지 판단하여 처리
                # 여기서는 로깅만 하고 진행하거나, 특정 오류 페이지 표시 가능
                error_message_for_template = "세션 처리 중 오류가 발생했습니다." 
        elif not session_id_from_query:
            logger.warning("[세션] sessionId 쿼리 파라미터가 없습니다. chatWidget.js 설정 확인 필요.")
        elif not user_id:
            logger.info("[세션] 사용자 ID가 확정되지 않아 세션 정보를 처리할 수 없습니다 (로그인 필요 등).")
            # login_required_for_template 가 True가 될 것이므로 별도 처리 불필요

        # 최종 컨텍스트 준비
        context = {
            "request": request,
            "user_info": retrieved_user_info,
            "error": error_message_for_template,
            "login_required": login_required_for_template,
            "auth_url": auth_url, # 인증 URL 전달
            "current_session_id": session_id_from_query # 현재 세션 ID도 전달 (선택 사항)
        }
        response = templates.TemplateResponse("index.html", context)
        
        # 쿠키 설정 (최신 값으로)
        if access_token:
            response.set_cookie("access_token", access_token, samesite="None", secure=True, httponly=True)
        if user_id and user_id != 'None':
            response.set_cookie("user_id", str(user_id), samesite="None", secure=True, httponly=True)
        else:
            response.delete_cookie("user_id", samesite="None", secure=True, httponly=True)
            
        return response
            
    except ImportError as e:
        logger.critical(f"[모듈 오류] 필수 모듈 가져오기 실패: {str(e)}", exc_info=True)
        return HTMLResponse(content="<h1>애플리케이션 설정 오류</h1><p>필수 모듈을 로드할 수 없습니다.</p>", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    except Exception as e:
        logger.error(f"[심각한 오류] index 함수 처리 중 예외 발생: {str(e)}", exc_info=True)
        # 만약 DB 롤백이 필요하다면, 이 최상위 예외 핸들러에서도 롤백 시도 고려
        # if db: await db.rollback() # 하지만 get_db에서 이미 처리하고 있을 수 있음
        return HTMLResponse(content="<h1>요청 처리 오류</h1><p>죄송합니다. 요청을 처리하는 중 내부 오류가 발생했습니다.</p>", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.post("/api/chat")
async def chat(payload: ChatMessage, db: AsyncSession = Depends(get_db), llm_client_dep: AsyncLLMMcpClient = Depends(get_llm_client)):
    """사용자 메시지를 처리하고 응답을 반환하며, DB 로그/요약 기반 컨텍스트를 사용하고 로그를 기록합니다."""
    try:
        user_message = payload.message
        user_id_from_client = payload.user_id
        raw_company_code = payload.user_company
        session_id_from_payload = payload.session_id
        image_data_from_payload = payload.image_data # 수신된 이미지 데이터

        # --- 입력 값 검증 (기존 유지) ---
        if not user_message and not image_data_from_payload: 
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="메시지 또는 이미지가 필요합니다.")
        if not user_id_from_client: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="사용자 ID가 필요합니다.")
        if not session_id_from_payload: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="세션 ID가 필요합니다.")
        
        company_code_int = DEFAULT_COMPANY_CODE
        if raw_company_code and raw_company_code.strip():
            try: company_code_int = int(raw_company_code)
            except ValueError: logger.warning(f"잘못된 company_code 형식: '{raw_company_code}'. 기본값 사용.")
        # --- 입력 값 검증 끝 ---
        
        logger.info(f"메시지 수신 - Session: {session_id_from_payload}, User: {user_id_from_client}, Company: {company_code_int}, Image present: {True if image_data_from_payload else False}")
        
        if image_data_from_payload:
            # TODO: 수신된 이미지 데이터 처리 로직 구현
            # 예: 파일로 저장, 특정 분석 서비스로 전송, LLM에 이미지 설명 요청 등
            logger.info(f"첨부된 이미지 데이터 수신 (첫 100자): {image_data_from_payload[:100]}...")
            # 여기서 image_data_from_payload를 사용하여 필요한 작업을 수행합니다.
            # 예를 들어, LLM에 이미지와 함께 user_message를 전달해야 할 수 있습니다.
            # 이 경우 llm_client_dep.process_user_input 함수의 시그니처 변경 또는 내부 로직 수정이 필요할 수 있습니다.

        if not llm_client_dep: # LLM 클라이언트 의존성 주입 확인
            logger.error("LLM 클라이언트 의존성 주입 실패 (None).")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="LLM 서비스가 현재 사용 불가능합니다.")

        # 1. 사용자 메시지 로그 기록 (항상 먼저)
        # 이미지 첨부 여부나 이미지에 대한 설명을 로그에 남길 수 있습니다.
        log_message_content = user_message
        if image_data_from_payload:
            log_message_content += " (이미지 첨부됨)" # 간단한 표시

        await chat_log_crud.create_chat_log(
            db=db, session_id=session_id_from_payload, user_id=user_id_from_client,
            message_type='user', message=log_message_content, company_code=company_code_int
        )
        logger.debug(f"User message logged - Session: {session_id_from_payload}")

        # 2. LLM 컨텍스트 구성 (요약 + 최근 로그)
        history_for_llm: List[Dict[str, Any]] = []
        current_summary_text: Optional[str] = None

        # 2.1. DB에서 기존 요약본 가져오기
        chat_summary = await chat_summary_crud.get_summary_by_session(db, session_id_from_payload)
        if chat_summary:
            current_summary_text = chat_summary.summary_text
            logger.info(f"기존 요약본 사용 - Session: {session_id_from_payload}")

        # 2.2. DB에서 채팅 로그 가져오기 (get_chat_logs_by_session은 전체 로그를 오름차순으로 반환)
        all_db_chat_logs = await chat_log_crud.get_chat_logs_by_session(db, session_id_from_payload)

        # 2.3. 요약 트리거 판단 및 새 요약 생성/업데이트
        #    - 현재는 단순하게 전체 로그 개수 기반으로 판단. 
        #    - 좀 더 정교하게는, (current_summary_text가 있을 경우) 요약 이후 쌓인 로그 개수, 또는 토큰 수 기반으로 판단 가능.
        
        logs_to_summarize = []
        if not current_summary_text and len(all_db_chat_logs) >= SUMMARY_TRIGGER_LOG_COUNT:
            # 요약본이 없고, 전체 로그가 요약 트리거 개수 이상이면 전체 로그를 요약 대상으로 함
            logs_to_summarize = all_db_chat_logs
            logger.info(f"요약 생성 트리거 (요약본 없음, 로그 {len(all_db_chat_logs)}개) - Session: {session_id_from_payload}")
        elif current_summary_text: 
            # 요약본이 있다면, 그 요약본이 커버하지 않는 새 로그들만 대상으로 할 수 있음.
            # 이를 위해선 ChatSummary에 last_summarized_log_id 같은 필드가 필요하나, 현재는 없음.
            # 임시 방편: 만약 all_db_chat_logs의 특정 개수(예: SUMMARY_TRIGGER_LOG_COUNT)가 요약본 생성 이후에 발생했다고 가정하고 처리.
            # 여기서는 단순화하여, 요약본이 있어도 주기적으로 전체를 다시 요약한다고 가정하거나,
            # 또는 최근 로그만 컨텍스트에 포함하고, 요약은 별도 로직(예: 배치)으로 돌린다고 가정.
            # 이번 구현에서는, 요약본이 있으면 일단 사용하고, "추가 요약"은 아래 process_user_input 호출 후 응답 받은 뒤에 고려.
            # (LLM 응답 받은 후, 전체 대화가 SUMMARY_TRIGGER_LOG_COUNT 보다 길면 그때 요약하여 다음 요청에 사용)
            pass # 요약본이 있으면 일단 넘어감 (아래 최근 로그만 컨텍스트에 포함)

        if logs_to_summarize: # 실제 요약 대상 로그가 있을 때만 요약 시도
            temp_history_for_summary = []
            for log in logs_to_summarize:
                temp_history_for_summary.append({"role": log.type, "content": log.message})
            
            if temp_history_for_summary: # 요약할 내용이 있을 때만
                new_summary = await llm_client_dep.summarize_conversation(
                    db=db, user_id=user_id_from_client, company_code=company_code_int,
                    history=temp_history_for_summary
                )
                if new_summary and new_summary.strip():
                    current_summary_text = new_summary # 새로 생성된 요약본으로 업데이트
                    await chat_summary_crud.create_or_update_summary(db, session_id_from_payload, current_summary_text)
                    logger.info(f"새 요약본 생성 및 저장 완료 - Session: {session_id_from_payload}")
                else:
                    logger.warning(f"요약 생성 실패 또는 빈 요약 반환 - Session: {session_id_from_payload}")
        
        # 2.4. LLM에 전달할 최근 로그 구성
        recent_logs_for_context = all_db_chat_logs[-MAX_RECENT_LOGS_FOR_CONTEXT:]
        for log in recent_logs_for_context:
            history_for_llm.append({"role": log.type, "content": log.message})
        
        logger.debug(f"Context for LLM (Summary: {True if current_summary_text else False}, Recent logs: {len(history_for_llm)}) - Session: {session_id_from_payload}")

        # 3. LLM 호출 (요약본은 chat_history의 일부가 아닌, 별도 파라미터나 시스템 메시지로 전달될 수 있음)
        #    process_user_input 함수 시그니처 변경 또는 내부 로직 수정 필요 가능성 있음.
        #    현재 process_user_input은 chat_history를 받으므로, 요약본을 여기에 어떻게 통합할지 결정.
        #    (1) 요약본을 system 메시지로 history_for_llm 맨 앞에 추가
        #    (2) process_user_input에 summary 파라미터 추가
        #    여기서는 (1)번 방식을 사용
        final_context_for_llm = []
        if current_summary_text:
            final_context_for_llm.append({"role": "system", "content": f"이전 대화 요약: {current_summary_text}"})
        final_context_for_llm.extend(history_for_llm) # history_for_llm에는 최근 대화만 포함됨

        response_data = await llm_client_dep.process_user_input(
            db=db, 
            user_id=user_id_from_client,
            company_code=company_code_int,
            user_input=user_message, # 현재 사용자 메시지
            chat_history=final_context_for_llm, # [요약(시스템) + 최근 대화] 전달
            image_data=image_data_from_payload # image_data 인자 추가 (process_user_input 함수 수정 필요)
        )
        logger.info(f"LLM 응답 생성 완료 - response_data: {response_data}")
        logger.info(f"LLM 응답 생성 완료 - Session: {session_id_from_payload}")

        assistant_message = response_data.get("text") if isinstance(response_data, dict) else str(response_data)
        if not assistant_message: assistant_message = "응답을 생성하지 못했습니다."

        # 4. 어시스턴트 응답 로그 기록
        await chat_log_crud.create_chat_log(
            db=db, session_id=session_id_from_payload, user_id=user_id_from_client,
            message_type='assistant', message=assistant_message, company_code=company_code_int # company_code 추가
        )
        logger.debug(f"Assistant response logged - Session: {session_id_from_payload}")

        # 5. 모든 DB 작업 성공 시 커밋
        await db.commit()
        logger.info(f"Chat transaction committed - Session: {session_id_from_payload}")
        # --- DB 작업 및 LLM 호출 완료 --- 

        final_response = response_data if isinstance(response_data, dict) else {"type": "text", "text": assistant_message}
        return final_response

    except HTTPException: 
        raise
    except Exception as e:
        logger.error(f"채팅 처리 중 심각한 오류 발생: {str(e)}", exc_info=True)
        if db: # db 세션이 유효하다면 롤백 시도
            try: await db.rollback(); logger.info("롤백 성공 (채팅 API 오류)")
            except Exception as rb_err: logger.error(f"롤백 중 추가 오류 발생: {rb_err}", exc_info=True)
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail=f"채팅 처리 중 서버 오류가 발생했습니다."
        )

@app.get("/api/servers")
async def get_servers():
    """사용 가능한 서버 목록을 반환하는 API 엔드포인트"""
    try:
        servers = []
        server_data = await load_server_list()
        
        # 모든 서버 정보를 가져와서 활성화 상태 추가
        for server in server_data.get("servers", []):
            server_name = server.get("name")
            server_url = server.get("url")
            server_enabled = server.get("enabled", True)
            
            # 툴 정보 가져오기
            tools = []
            if server_enabled:  # 활성화된 서버만 툴 정보 가져오기
                try:
                    # 캐시된 클라이언트가 있으면 사용, 없으면 새로 생성
                    if server_name in server_clients and server_clients[server_name].initialized:
                        client = server_clients[server_name]
                        tools = await client.get_tools()
                    else:
                        # McpSseClient를 사용하여 도구 목록 가져오기
                        client = McpSseClient(server_url)
                        connected = await client.connect()
                        if connected:
                            tools = await client.get_tools()
                            # 클라이언트 캐싱
                            server_clients[server_name] = client
                except Exception as e:
                    logger.error(f"서버 {server_name} 툴 정보 가져오기 실패: {str(e)}")
            
            servers.append({
                "name": server_name,
                "url": server_url,
                "enabled": server_enabled,
                "tools": tools
            })
            
        return {"servers": servers}
    except Exception as e:
        logger.error(f"서버 목록 조회 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"오류가 발생했습니다: {str(e)}")

@app.post("/api/servers/add")
async def add_server(server: ServerInfo, llm_client: AsyncLLMMcpClient = Depends(get_llm_client)):
    """새 서버를 추가하는 API 엔드포인트"""
    try:
        name = server.name
        url = server.url
        
        if not name or not url:
            return JSONResponse(
                content={"success": False, "error": "서버 이름과 URL이 필요합니다."},
                status_code=400
            )
            
        # 서버 목록 로드
        server_data = await load_server_list()
        
        # "servers" 키가 없으면 생성
        if "servers" not in server_data:
            server_data["servers"] = []
        
        # 동일한 이름의 서버가 있는지 확인
        for i, srv in enumerate(server_data["servers"]):
            if srv.get("name") == name:
                # 기존 서버 업데이트
                server_data["servers"][i] = {
                    "name": name,
                    "url": url,
                    "enabled": True
                }
                break
        else:
            # 새 서버 추가
            server_data["servers"].append({
                "name": name,
                "url": url,
                "enabled": True
            })
        
        # 서버 목록 저장
        if await save_server_list(server_data):
            # LLM 클라이언트 서버 맵 및 툴 목록 갱신
            llm_client.server_map = await get_server_map()
            await llm_client.initialize_tools()
            
            # 기존 클라이언트 제거 (새로 연결하도록)
            if name in server_clients:
                del server_clients[name]
            
            logger.info(f"서버 '{name}' 추가됨: {url}")
            
            return {"success": True}
        else:
            return JSONResponse(
                content={"success": False, "error": "서버 목록 저장 실패"},
                status_code=500
            )
            
    except Exception as e:
        logger.error(f"서버 추가 중 오류 발생: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.post("/api/servers/toggle")
async def toggle_server(toggle_data: ToggleServer, llm_client: AsyncLLMMcpClient = Depends(get_llm_client)):
    """서버 활성화/비활성화 상태를 변경하는 API 엔드포인트"""
    try:
        name = toggle_data.name
        enabled = toggle_data.enabled
        
        if name is None or enabled is None:
            return JSONResponse(
                content={"success": False, "error": "서버 이름과 활성화 상태가 필요합니다."},
                status_code=400
            )
            
        # 서버 목록 로드
        server_data = await load_server_list()
        
        # 서버 찾기
        found = False
        if "servers" in server_data:
            for i, server in enumerate(server_data["servers"]):
                if server.get("name") == name:
                    # 서버 상태 업데이트
                    server_data["servers"][i]["enabled"] = enabled
                    found = True
                    break
        
        if not found:
            return JSONResponse(
                content={"success": False, "error": f"서버 '{name}'을 찾을 수 없습니다."},
                status_code=404
            )
        
        # 서버 목록 저장
        if await save_server_list(server_data):
            # LLM 클라이언트 서버 맵 및 툴 목록 갱신
            llm_client.server_map = await get_server_map()
            await llm_client.initialize_tools()
            
            # 비활성화 시 클라이언트 제거
            if not enabled and name in server_clients:
                del server_clients[name]
            
            logger.info(f"서버 '{name}' 상태 변경됨: {enabled}")
            
            return {"success": True}
        else:
            return JSONResponse(
                content={"success": False, "error": "서버 목록 저장 실패"},
                status_code=500
            )
            
    except Exception as e:
        logger.error(f"서버 상태 변경 중 오류 발생: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

@app.get("/api/tools/{server_name}")
async def get_tools(server_name: str):
    """특정 서버의 사용 가능한 툴 목록을 반환하는 API 엔드포인트"""
    try:
        # 캐시된 클라이언트가 있으면 사용, 없으면 새로 생성
        client = await get_or_create_mcp_client(server_name)
        tools = await client.get_tools()
        
        return {"tools": tools}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"툴 목록 조회 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"오류가 발생했습니다: {str(e)}")

@app.post("/api/tools/invoke/{server_name}")
async def invoke_tool(server_name: str, tool_request: ToolRequest):
    """특정 서버의 툴을 호출하는 API 엔드포인트"""
    try:
        # 캐시된 클라이언트가 있으면 사용, 없으면 새로 생성
        client = await get_or_create_mcp_client(server_name)
        
        # 도구 호출
        result = await client.invoke_tool(tool_request.tool_name, tool_request.inputs)
        
        if result is None:
            raise HTTPException(status_code=500, detail=f"도구 호출 실패: {tool_request.tool_name}")
        
        # 대기 상태 확인 (비동기 응답 처리)
        if isinstance(result, dict) and result.get("status") == "waiting":
            # 클라이언트에게 대기 상태 알림
            return {
                "status": "waiting", 
                "message": f"도구 {tool_request.tool_name} 호출 중. 결과가 준비되면 표시됩니다."
            }
        
        # 오류 응답 확인
        if isinstance(result, dict) and "error" in result:
            # 오류 정보를 클라이언트에게 전달
            logger.error(f"도구 호출 오류: {result['error']}")
            return {"status": "error", "error": result["error"]}
        
        return {"result": result, "status": "success"}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"도구 호출 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"오류가 발생했습니다: {str(e)}")

@app.post("/api/servers/delete")
async def delete_server(delete_data: DeleteServer, llm_client: AsyncLLMMcpClient = Depends(get_llm_client)):
    """서버를 삭제하는 API 엔드포인트"""
    try:
        name = delete_data.name
        
        if not name:
            return JSONResponse(
                content={"success": False, "error": "서버 이름이 필요합니다."},
                status_code=400
            )
            
        # 서버 목록 로드
        server_data = await load_server_list()
        
        # 서버 찾기
        found = False
        if "servers" in server_data:
            for i, server in enumerate(server_data["servers"]):
                if server.get("name") == name:
                    # 서버 삭제
                    del server_data["servers"][i]
                    found = True
                    break
        
        if not found:
            return JSONResponse(
                content={"success": False, "error": f"서버 '{name}'을 찾을 수 없습니다."},
                status_code=404
            )
        
        # 서버 목록 저장
        if await save_server_list(server_data):
            # LLM 클라이언트 서버 맵 및 툴 목록 갱신
            llm_client.server_map = await get_server_map()
            await llm_client.initialize_tools()
            
            # 클라이언트 캐시에서 제거
            if name in server_clients:
                del server_clients[name]
            
            logger.info(f"서버 '{name}' 삭제됨")
            
            return {"success": True}
        else:
            return JSONResponse(
                content={"success": False, "error": "서버 목록 저장 실패"},
                status_code=500
            )
            
    except Exception as e:
        logger.error(f"서버 삭제 중 오류 발생: {str(e)}")
        return JSONResponse(
            content={"success": False, "error": str(e)},
            status_code=500
        )

# SSE 스트림 엔드포인트
@app.get("/api/sse-stream/{server_name}")
async def sse_stream(server_name: str, request: Request):
    """SSE 스트림을 제공하는 엔드포인트"""
    try:
        # 서버 맵에서 서버 URL 가져오기
        server_map = await get_server_map()
        server_url = server_map.get(server_name)
        if not server_url:
            raise HTTPException(status_code=404, detail=f"서버를 찾을 수 없습니다: {server_name}")
        
        # SSE 엔드포인트 URL 생성
        sse_url = f"{server_url.rstrip('/')}/sse"
        
        async def event_generator():
            try:
                async with httpx.AsyncClient() as client:
                    async with client.stream('GET', sse_url, timeout=None) as response:
                        response.raise_for_status()
                        event_type = None
                        
                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                                
                            # 이벤트 타입 추출
                            if line.startswith("event:"):
                                event_type = line[6:].strip()
                                continue
                            
                            # 데이터 추출 및 전달
                            if line.startswith("data:"):
                                data = line[5:].strip()
                                yield {
                                    "event": event_type or "message",
                                    "data": data
                                }
            except Exception as e:
                logger.error(f"SSE 스트림 에러: {str(e)}")
                yield {
                    "event": "error",
                    "data": str(e)
                }
        
        return EventSourceResponse(event_generator())
    except Exception as e:
        logger.error(f"SSE 스트림 설정 중 오류 발생: {str(e)}")
        raise HTTPException(status_code=500, detail=f"SSE 스트림 설정 실패: {str(e)}")

async def get_tool_list(server_url: str) -> List[dict]:
    """서버에서 도구 목록을 가져옵니다.
    
    Args:
        server_url: 서버 URL
        
    Returns:
        도구 목록
    """
    global llm_client
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            client = McpSseClient(server_url)
            if llm_client:
                client.llm_client = llm_client
                
            tools = await client.get_tools()
            if tools:
                logger.info(f"도구 목록 가져오기 성공 (시도 {attempt + 1}/{max_retries})")
                return tools
            else:
                logger.warning(f"빈 도구 목록 수신 (시도 {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (2 ** attempt))
                    continue
                    
        except Exception as e:
            logger.error(f"도구 목록 가져오기 실패 (시도 {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (2 ** attempt))
                continue
                
    logger.error("최대 재시도 횟수 초과")
    return []

# --- 가상의 사용자 컨텍스트 의존성 --- 
# 실제 구현에서는 JWT 토큰 검증, 세션 확인 등을 통해 user_id와 company_code를 가져와야 함
class UserContext(BaseModel):
    user_id: str
    company_code: int

async def get_current_user_context(request: Request) -> UserContext:
    # 여기서는 임시로 쿠키에서 값을 읽어옴 (실제로는 보안 검증 필요)
    user_id = request.cookies.get("user_id")
    # company_code를 어떻게 가져올지 정의 필요 - 여기서는 user_info 조회 또는 다른 쿠키 사용 가정
    # 예시: access_token으로 user_info 조회 후 company_code 추출
    access_token = request.cookies.get("access_token")
    company_code = 0 # 기본값 또는 오류 값
    if access_token and user_id:
        try:
            user_info = console_service.get_user_info(access_token) 
            if user_info and isinstance(user_info.get('recv'), dict):
                code = user_info['recv'].get('company') 
                if code is not None:
                    company_code = int(code) 
        except Exception as e:
            logger.warning(f"사용자 정보에서 company_code 추출 실패: {e}")
            pass # 기본값 0 사용

    if not user_id:
        raise HTTPException(status_code=401, detail="인증되지 않은 사용자입니다.")
    
    return UserContext(user_id=user_id, company_code=company_code)

@app.get("/api/prompts")
async def get_prompts_from_db(user_ctx: UserContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    """현재 사용자의 커스텀 프롬프트와 시스템 기본 프롬프트를 DB에서 가져옵니다."""
    try:
        logger.info(f"DB 기반 프롬프트 목록 조회 시작 - User: {user_ctx.user_id}, Company: {user_ctx.company_code}")
        prompts_formatted = await prompt_crud.get_all_prompts_formatted(db, user_ctx.user_id, user_ctx.company_code)
        logger.info(f"DB 기반 프롬프트 목록 조회 완료")
        return {"prompts": prompts_formatted}
    except Exception as e:
        logger.error(f"DB 프롬프트 로드 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB 프롬프트 로드 실패: {str(e)}")

@app.get("/api/prompts/{prompt_type}")
async def get_prompt_from_db(prompt_type: str, user_ctx: UserContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    """특정 타입의 유효 프롬프트를 DB에서 가져옵니다 (커스텀 우선)."""
    try:
        logger.info(f"DB 기반 프롬프트 조회 시작 - Type: {prompt_type}, User: {user_ctx.user_id}, Company: {user_ctx.company_code}")
        prompt_text = await prompt_crud.get_effective_prompt(db, user_ctx.user_id, user_ctx.company_code, prompt_type)
        logger.info(f"DB 기반 프롬프트 조회 완료 - Type: {prompt_type}")
        return {"prompt": prompt_text if prompt_text is not None else ""} # 결과가 None이면 빈 문자열 반환
    except Exception as e:
        logger.error(f"DB 프롬프트 로드 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DB 프롬프트 로드 실패: {str(e)}")

@app.post("/api/prompts/{prompt_type}")
async def save_prompt_to_db(prompt_type: str, prompt_request: PromptRequest, user_ctx: UserContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    try:
        logger.info(f"DB 프롬프트 저장/업데이트 시작 - Type: {prompt_type}, User: {user_ctx.user_id}, Company: {user_ctx.company_code}")
        saved_prompt = await prompt_crud.save_prompt(
            db, 
            user_id=user_ctx.user_id, 
            company_code=user_ctx.company_code, 
            prompt_type=prompt_type, 
            prompt_text=prompt_request.prompt
        )
        await db.commit() # prompt_crud.save_prompt 성공 후 커밋
        logger.info(f"DB 프롬프트 저장/업데이트 완료 - ID: {saved_prompt.idx}, Type: {prompt_type}")
        return {"success": True}
    except Exception as e:
        if db: await db.rollback()
        logger.error(f"DB 프롬프트 저장 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DB 프롬프트 저장 실패: {str(e)}")

@app.delete("/api/prompts/{prompt_type}")
async def delete_prompt_from_db(prompt_type: str, user_ctx: UserContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    try:
        logger.info(f"DB 프롬프트 삭제 시작 - Type: {prompt_type}, User: {user_ctx.user_id}, Company: {user_ctx.company_code}")
        deleted = await prompt_crud.delete_prompt(
            db, 
            user_id=user_ctx.user_id, 
            company_code=user_ctx.company_code, 
            prompt_type=prompt_type
        )
        await db.commit() # prompt_crud.delete_prompt 성공 후 커밋 (rowcount > 0 여부와 관계없이 트랜잭션 종료)
        
        if deleted:
            logger.info(f"DB 프롬프트 삭제 완료 - Type: {prompt_type}")
            return {"success": True}
        else:
            logger.info(f"삭제할 DB 프롬프트 없음 - Type: {prompt_type}")
            return {"success": True, "message": "Prompt not found or already deleted."}
    except Exception as e:
        if db: await db.rollback()
        logger.error(f"DB 프롬프트 삭제 중 오류: {str(e)}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"DB 프롬프트 삭제 실패: {str(e)}")

# --- 대화 요약 API 엔드포인트 (추가 또는 수정) ---
@app.post("/api/chat/summarize")
async def summarize_chat_api(
    payload: SummarizeRequest, 
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    llm_client_dep: AsyncLLMMcpClient = Depends(get_llm_client)
):
    try:
        if not llm_client_dep:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="LLM 서비스가 현재 사용 불가능합니다.")

        logger.info(f"대화 요약 API 호출 - User: {user_ctx.user_id}, Company: {user_ctx.company_code}, History items: {len(payload.history)}")
        summary = await llm_client_dep.summarize_conversation(
            db=db,
            user_id=user_ctx.user_id,
            company_code=user_ctx.company_code,
            history=payload.history
        )
        
        if summary is None:
            logger.warning(f"대화 요약 실패 (결과 None) - User: {user_ctx.user_id}")
            # 이 경우, 클라이언트에게 오류를 알리기 위해 HTTP 예외를 발생시킬 수 있음
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="요약 생성에 실패했습니다.")
        
        logger.info(f"대화 요약 성공 - User: {user_ctx.user_id}")
        return {"summary": summary}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"대화 요약 API 처리 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"요약 처리 중 서버 오류 발생: {e}")

# --- 이전 대화 기록 로드 API 엔드포인트 ---
class ChatLogHistoryItem(BaseModel):
    role: str
    content: str
    timestamp: datetime

class ChatHistoryResponse(BaseModel):
    logs: List[ChatLogHistoryItem]

@app.get("/api/chat/history/{session_id}", response_model=ChatHistoryResponse)
async def get_chat_history(
    session_id: str, 
    db: AsyncSession = Depends(get_db)
    # user_ctx: UserContext = Depends(get_current_user_context) # 필요시 주석 해제하여 사용자 검증 추가
):
    """특정 세션 ID의 모든 채팅 기록을 시간 오름차순으로 반환합니다."""
    try:
        logger.info(f"이전 대화 기록 조회 요청 - Session ID: {session_id}")
        
        db_chat_logs = await chat_log_crud.get_chat_logs_by_session(db, session_id)
        
        if not db_chat_logs:
            logger.info(f"해당 세션에 대한 대화 기록 없음 - Session ID: {session_id}")
            return ChatHistoryResponse(logs=[])

        history_items: List[ChatLogHistoryItem] = []
        for log in db_chat_logs:
            history_items.append(
                ChatLogHistoryItem(
                    role=log.type, # ChatLog 모델의 'type' 컬럼 사용
                    content=log.message,
                    timestamp=log.reg_date
                )
            )
        
        logger.info(f"이전 대화 기록 {len(history_items)}건 조회 완료 - Session ID: {session_id}")
        return ChatHistoryResponse(logs=history_items)

    except Exception as e:
        logger.error(f"이전 대화 기록 조회 중 오류 발생 - Session ID: {session_id}: {str(e)}", exc_info=True)
        # 클라이언트에게는 일반적인 오류 메시지 반환
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="이전 대화 기록을 가져오는 중 오류가 발생했습니다."
        )
# --- 이전 대화 기록 로드 API 엔드포인트 끝 ---

# --- New SSE Endpoint ---
async def log_event_generator(request: Request):
    """Async generator for SSE log events, reads from queue."""
    logger.info("SSE client connected to /mcp-agent/logs")
    queue_get_count = 0 # To prevent infinite loops if queue is broken
    MAX_QUEUE_CHECKS = 10 # Check disconnect after this many timeouts
    
    while True:
        if await request.is_disconnected():
            logger.info("SSE client disconnected")
            break
        try:
            # Wait for a log message from the queue with timeout
            log_data = await asyncio.wait_for(log_queue.get(), timeout=1.0)
            queue_get_count = 0 # Reset counter on success
            
            # Yield the data (EventSourceResponse handles formatting)
            yield {"data": json.dumps(log_data)}
            log_queue.task_done() # Mark task as done
            
        except asyncio.TimeoutError:
            # No log received, check disconnect periodically
            queue_get_count += 1
            if queue_get_count >= MAX_QUEUE_CHECKS:
                if await request.is_disconnected():
                    logger.info("SSE client disconnected during timeout checks")
                    break
                else:
                    queue_get_count = 0 # Reset counter if still connected
            continue # Continue waiting
            
        except asyncio.CancelledError:
            logger.info("SSE generator cancelled")
            break
        except Exception as e:
            logger.error(f"Error in SSE generator: {e}", exc_info=True)
            try:
                # Try yielding an error to the client
                yield {"event": "error", "data": json.dumps({"message": "SSE stream error"})}
            except Exception:
                pass # Ignore error yielding error
            break # Stop streaming on error

@app.get("/logs")
async def sse_logs(request: Request):
    """SSE endpoint for streaming logs from the LLM client."""
    if llm_client is None:
        logger.warning("SSE endpoint /mcp-agent/logs called but LLM client is None.")
        pass 
    return EventSourceResponse(log_event_generator(request))

# --- 팝업 페이지 라우트 ---
@app.get("/server-tool-popup", response_class=HTMLResponse)
async def server_tool_popup(request: Request):
    """서버/툴 관리 팝업 HTML을 반환합니다."""
    return templates.TemplateResponse("server_tool_popup.html", {"request": request})

@app.get("/prompt-popup", response_class=HTMLResponse)
async def prompt_popup(request: Request):
    """프롬프트 관리 팝업 HTML을 반환합니다."""
    return templates.TemplateResponse("prompt_popup.html", {"request": request})

@app.get("/debug-popup", response_class=HTMLResponse)
async def debug_popup(request: Request):
    """디버그 팝업 HTML을 반환합니다."""
    return templates.TemplateResponse("debug_popup.html", {"request": request})

@app.get("/sample", response_class=HTMLResponse)
async def sample_page(request: Request):
    """샘플 페이지 HTML을 반환합니다."""
    return templates.TemplateResponse("sample_page.html", {"request": request})
# --- End Popup Routes ---

# 세션 관련 Pydantic 모델 정의
class SessionUpdateRequest(BaseModel):
    session_id: str
    session_title: str

class SessionInfoResponse(BaseModel):
    session_id: str
    session_title: Optional[str] = None
    reg_date: datetime
    user_id: str

class SessionListResponse(BaseModel):
    sessions: List[SessionInfoResponse]

class DeleteSessionRequest(BaseModel):
    session_id: str

# 세션 리스트 조회 API 추가
@app.get("/api/sessions", response_model=SessionListResponse)
async def get_sessions(user_ctx: UserContext = Depends(get_current_user_context), db: AsyncSession = Depends(get_db)):
    """현재 사용자의 모든 세션 목록을 반환합니다."""
    try:
        logger.info(f"사용자 세션 목록 조회 - User ID: {user_ctx.user_id}")
        
        # 사용자의 모든 세션 가져오기 (company_code로 필터링 선택 가능)
        sessions = await session_crud.get_all_sessions_by_user(db, user_ctx.user_id, user_ctx.company_code)
        
        # 응답 모델로 변환
        session_responses = []
        for session in sessions:
            session_responses.append(
                SessionInfoResponse(
                    session_id=session.session_id,
                    session_title=session.session_title,
                    reg_date=session.reg_date,
                    user_id=session.user_id
                )
            )
        
        logger.info(f"세션 목록 {len(session_responses)}건 조회 완료 - User ID: {user_ctx.user_id}")
        return SessionListResponse(sessions=session_responses)

    except Exception as e:
        logger.error(f"세션 목록 조회 중 오류 발생 - User ID: {user_ctx.user_id}: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="세션 목록을 가져오는 중 오류가 발생했습니다."
        )

# 세션 제목 업데이트 API 추가
@app.put("/api/sessions/title", status_code=status.HTTP_200_OK)
async def update_session_title(
    payload: SessionUpdateRequest, 
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """세션의 제목을 업데이트합니다."""
    try:
        logger.info(f"세션 제목 업데이트 요청 - Session ID: {payload.session_id}, New Title: {payload.session_title}")
        
        # 세션이 사용자의 소유인지 확인 (선택적 보안 조치)
        session = await session_crud.get_session_by_id(db, payload.session_id)
        if not session:
            logger.warning(f"세션을 찾을 수 없음 - Session ID: {payload.session_id}")
            raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다.")
        
        if session.user_id != user_ctx.user_id:
            logger.warning(f"세션 접근 권한 없음 - Session ID: {payload.session_id}, Request User: {user_ctx.user_id}, Session Owner: {session.user_id}")
            raise HTTPException(status_code=403, detail="해당 세션에 대한 접근 권한이 없습니다.")
        
        # 세션 제목 업데이트
        updated_session = await session_crud.update_session_title(db, payload.session_id, payload.session_title)
        if not updated_session:
            logger.error(f"세션 제목 업데이트 실패 - Session ID: {payload.session_id}")
            raise HTTPException(status_code=500, detail="세션 제목 업데이트에 실패했습니다.")
        
        # 변경사항 저장
        await db.commit()
        logger.info(f"세션 제목 업데이트 완료 - Session ID: {payload.session_id}, New Title: {payload.session_title}")
        
        return {"success": True, "message": "세션 제목이 업데이트되었습니다."}
    
    except HTTPException:
        # 이미 적절한 HTTP 응답으로 변환된 예외는 그대로 전달
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"세션 제목 업데이트 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="세션 제목 업데이트 중 서버 오류가 발생했습니다.")

# 세션 삭제 API 추가
@app.delete("/api/sessions", status_code=status.HTTP_200_OK)
async def delete_session(
    payload: DeleteSessionRequest,
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """세션을 삭제합니다. 관련된 채팅 로그와 요약 정보도 함께 삭제됩니다."""
    try:
        logger.info(f"세션 삭제 요청 - Session ID: {payload.session_id}")
        
        # 세션이 사용자의 소유인지 확인
        session = await session_crud.get_session_by_id(db, payload.session_id)
        if not session:
            logger.warning(f"삭제할 세션을 찾을 수 없음 - Session ID: {payload.session_id}")
            raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다.")
        
        if session.user_id != user_ctx.user_id:
            logger.warning(f"세션 삭제 권한 없음 - Session ID: {payload.session_id}, Request User: {user_ctx.user_id}, Session Owner: {session.user_id}")
            raise HTTPException(status_code=403, detail="해당 세션을 삭제할 권한이 없습니다.")
        
        # 세션과 관련된 모든 데이터 삭제 (트랜잭션으로 처리)
        try:
            # 1. 세션에 연결된 채팅 로그 삭제
            # 2. 세션에 연결된 요약 정보 삭제
            # 3. 세션 정보 삭제
            # (이 기능들은 crud 모듈에 추가 구현 필요)
            
            # 임시로 해당 세션 ID를 가진 행을 직접 삭제하는 SQL 실행
            await db.execute(f"DELETE FROM tb_chat_log WHERE session_id = '{payload.session_id}'")
            await db.execute(f"DELETE FROM tb_chat_summary WHERE session_id = '{payload.session_id}'")
            await db.execute(f"DELETE FROM tb_session_info WHERE session_id = '{payload.session_id}'")
            
            await db.commit()
            logger.info(f"세션 및 관련 데이터 삭제 완료 - Session ID: {payload.session_id}")
            
            return {"success": True, "message": "세션이 성공적으로 삭제되었습니다."}
        
        except Exception as e_delete:
            await db.rollback()
            logger.error(f"세션 데이터 삭제 중 오류 발생: {str(e_delete)}", exc_info=True)
            raise HTTPException(status_code=500, detail="세션 데이터 삭제 중 오류가 발생했습니다.")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"세션 삭제 처리 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="세션 삭제 중 서버 오류가 발생했습니다.")

@app.put("/api/chat/session/{session_id}", status_code=status.HTTP_200_OK)
async def update_chat_session(
    session_id: str,
    payload: dict,
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """세션 정보를 업데이트합니다."""
    try:
        logger.info(f"세션 정보 업데이트 요청 - Session ID: {session_id}")
        
        session_title = payload.get("session_title")
        if not session_title:
            raise HTTPException(status_code=400, detail="세션 제목이 필요합니다.")
        
        # 세션이 사용자의 소유인지 확인
        session = await session_crud.get_session_by_id(db, session_id)
        if not session:
            logger.warning(f"세션을 찾을 수 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다.")
        
        if session.user_id != user_ctx.user_id:
            logger.warning(f"세션 접근 권한 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=403, detail="해당 세션에 대한 접근 권한이 없습니다.")
        
        # 세션 제목 업데이트
        updated_session = await session_crud.update_session_title(db, session_id, session_title)
        if not updated_session:
            raise HTTPException(status_code=500, detail="세션 제목 업데이트에 실패했습니다.")
        
        await db.commit()
        logger.info(f"세션 제목 업데이트 완료 - Session ID: {session_id}, New Title: {session_title}")
        
        return {"success": True, "message": "세션 제목이 업데이트되었습니다."}
    
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"세션 정보 업데이트 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="세션 정보 업데이트 중 서버 오류가 발생했습니다.")

@app.post("/api/chat/session", status_code=status.HTTP_201_CREATED)
async def create_chat_session(
    payload: dict,
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """새 세션을 생성합니다."""
    try:
        session_id = payload.get("session_id")
        session_title = payload.get("session_title", "새 대화")
        
        if not session_id:
            raise HTTPException(status_code=400, detail="세션 ID가 필요합니다.")
        
        session = await session_crud.create_session(
            db, 
            session_id=session_id, 
            user_id=user_ctx.user_id, 
            company_code=user_ctx.company_code,
            session_title=session_title
        )
        
        await db.commit()
        logger.info(f"새 세션 생성 완료 - Session ID: {session_id}, Title: {session_title}")
        
        return {"success": True, "session_id": session_id}
    
    except Exception as e:
        await db.rollback()
        logger.error(f"세션 생성 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="세션 생성 중 서버 오류가 발생했습니다.")

@app.delete("/api/chat/session/{session_id}", status_code=status.HTTP_200_OK)
async def delete_chat_session(
    session_id: str,
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """세션을 삭제합니다."""
    try:
        # 세션이 사용자의 소유인지 확인
        session = await session_crud.get_session_by_id(db, session_id)
        if not session:
            logger.warning(f"삭제할 세션을 찾을 수 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다.")
        
        if session.user_id != user_ctx.user_id:
            logger.warning(f"세션 삭제 권한 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=403, detail="해당 세션을 삭제할 권한이 없습니다.")
        
        # 세션과 관련된 모든 데이터 삭제
        await db.execute(f"DELETE FROM tb_chat_log WHERE session_id = '{session_id}'")
        await db.execute(f"DELETE FROM tb_chat_summary WHERE session_id = '{session_id}'")
        await db.execute(f"DELETE FROM tb_session_info WHERE session_id = '{session_id}'")
        
        await db.commit()
        logger.info(f"세션 및 관련 데이터 삭제 완료 - Session ID: {session_id}")
        
        return {"success": True, "message": "세션이 성공적으로 삭제되었습니다."}
    
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"세션 삭제 처리 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="세션 삭제 중 서버 오류가 발생했습니다.")

@app.get("/api/chat/session/{session_id}", status_code=status.HTTP_200_OK)
async def get_session_info(
    session_id: str,
    user_ctx: UserContext = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db)
):
    """세션 정보를 조회합니다."""
    try:
        logger.info(f"세션 정보 조회 요청 - Session ID: {session_id}")
        
        # 세션 정보 조회
        session = await session_crud.get_session_by_id(db, session_id)
        if not session:
            logger.warning(f"세션을 찾을 수 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=404, detail="해당 세션을 찾을 수 없습니다.")
        
        # 사용자 권한 확인 (옵션)
        if session.user_id != user_ctx.user_id:
            logger.warning(f"세션 접근 권한 없음 - Session ID: {session_id}")
            raise HTTPException(status_code=403, detail="해당 세션에 대한 접근 권한이 없습니다.")
        
        # 세션 정보 반환
        return {
            "session_id": session.session_id,
            "session_title": session.session_title,
            "user_id": session.user_id,
            "reg_date": session.reg_date.isoformat(),
            "company_code": session.company_code
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"세션 정보 조회 중 오류 발생: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"세션 정보 조회 중 서버 오류가 발생했습니다.")
