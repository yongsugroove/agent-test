import os
import json
import logging
import traceback
import asyncio
from pathlib import Path
from typing import Dict, List, Any, Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI
from async_mcp_client import call_mcp_tool, load_server_list, get_registered_tools

try:
    from crud import prompt_crud
    from sqlalchemy.ext.asyncio import AsyncSession # get_prompt에서 사용
except ImportError:
    import sys
    SCRIPT_DIR_PARENT = Path(__file__).resolve().parent.parent
    sys.path.append(str(SCRIPT_DIR_PARENT)) # 프로젝트 루트를 sys.path에 추가 가정
    from crud import prompt_crud
    from sqlalchemy.ext.asyncio import AsyncSession # get_prompt에서 사용

# 스크립트 파일이 있는 디렉토리 경로
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE_PATH = SCRIPT_DIR / 'mcp_client.log' # 기존 유지

class ToolNotFoundError(Exception):
    """도구를 찾을 수 없을 때 발생하는 예외"""
    pass

# 환경 변수 로드
load_dotenv()

# --- SseLogHandler 클래스 정의 시작 ---
class SseLogHandler(logging.Handler):
    """로그 레코드를 SSE 콜백으로 전송하는 커스텀 핸들러"""
    def __init__(self, client_instance):
        super().__init__()
        self.client_instance = client_instance
        # 핸들러 자체의 레벨을 DEBUG로 설정하여 모든 레벨의 로그를 처리하도록 함
        self.setLevel(logging.DEBUG)

    def emit(self, record):
        """로그 레코드를 포맷하고 콜백 함수로 비동기 전송합니다."""
        logger.debug(">>> SseLogHandler.emit called") # 디버깅 print 추가
        callback = self.client_instance.logging_callback # 명확성을 위해 로컬 변수에 저장
        logger.debug(f">>> Current logging_callback value: {callback}") # 콜백 값 확인 로그 추가
        logger.debug(f">>> Boolean evaluation of callback: {bool(callback)}") # <<< 콜백의 불리언 값 확인 로그 추가
        if self.client_instance and callback: # 로컬 변수 사용
            try:
                # log_type 속성이 있는지 확인하여 type 결정 (getattr 사용, 없으면 'log' 기본값)
                log_type = getattr(record, 'log_type', 'log') 
                log_entry = {
                    'type': log_type, # <<< log_type 속성 또는 'log' 사용
                    'timestamp': record.created, # <<< record.created 사용으로 변경
                    'level': record.levelname,
                    'message': self.format(record), # 포맷된 메시지 사용
                    'name': record.name,
                    'pathname': record.pathname,
                    'lineno': record.lineno,
                }
                # 콜백 호출 시 log_type 사용
                logger.debug(f">>> Calling logging_callback with type: {log_type}") # 디버깅 print 추가
                asyncio.create_task(callback(log_type, log_entry)) # 로컬 변수 사용
            except Exception as e:
                logger.error(f"SseLogHandler 오류: {e}")
                self.handleError(record) # 기본 에러 핸들링

# --- SseLogHandler 클래스 정의 끝 ---

def setup_unified_logging():
    """통합된 로깅 설정을 초기화합니다."""
    # 기존 핸들러 제거
    root_logger = logging.getLogger()
    # 핸들러 제거 전에 핸들러 리스트 복사
    handlers_to_remove = root_logger.handlers[:]
    for handler in handlers_to_remove:
        # 핸들러 닫기 (파일 핸들러 등 리소스 해제)
        if hasattr(handler, 'close') and callable(handler.close):
            handler.close()
        root_logger.removeHandler(handler)

    # 로깅 포맷 설정
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 콘솔 핸들러 추가
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.DEBUG)

    # 파일 핸들러 추가 (절대 경로 사용)
    # 파일 핸들러 생성 전에 파일 경로가 포함된 디렉토리가 있는지 확인 및 생성
    LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    # 루트 로거 설정
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # 서드파티 라이브러리 로깅 레벨 조정
    logging.getLogger('httpx').setLevel(logging.DEBUG)
    logging.getLogger('asyncio').setLevel(logging.DEBUG)

# 통합 로깅 설정 적용
setup_unified_logging()
logger = logging.getLogger("mcp_client")

# OpenAI API 키 설정
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    # API 키가 없으면 오류를 발생시키는 대신 경고 로깅 (필요에 따라 조정)
    logger.warning("OPENAI_API_KEY가 설정되지 않았습니다. LLM 기능이 제한될 수 있습니다.")
    # raise ValueError("OPENAI_API_KEY가 설정되지 않았습니다.")

class AsyncLLMMcpClient:
    def __init__(self):
        self.server_map = {}
        self.registered_tools = {}
        self.client = None
        if OPENAI_API_KEY:
            self.client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        
        self.logger = logging.getLogger("mcp_client.llm")
        self.logger.setLevel(logging.DEBUG)
        self.conversation_history = []
        self.logging_callback = None

        self.sse_log_handler = SseLogHandler(self)
        self.logger.addHandler(self.sse_log_handler)
        self.logger.debug(f">>> LLM Client initialized in handler: ID={id(self)}")

    def add_to_history(self, role: str, content: str):
        """대화 기록에 새로운 메시지를 추가합니다."""
        self.conversation_history.append({
            "role": role,
            "content": content
        })

    def clear_history(self):
        """대화 기록을 초기화합니다."""
        self.conversation_history = []

    def set_logging_callback(self, callback):
        """SSE 로깅을 위한 비동기 콜백 함수를 설정합니다."""
        self.logging_callback = callback
        self.logger.info(f"SSE 로깅 콜백 설정됨: {callback}")

    async def get_prompt(self, db: AsyncSession, user_id: str, company_code: int, prompt_type: str) -> str:
        """
        DB에서 특정 타입의 유효 프롬프트를 직접 가져옵니다 (사용자/회사 커스텀 우선).
        """
        self.logger.debug(f"DB에서 프롬프트 조회 시작 - Type: {prompt_type}, User: {user_id}, Company: {company_code}")
        try:
            prompt_text = await prompt_crud.get_effective_prompt(db, user_id, company_code, prompt_type)
            if prompt_text is not None:
                self.logger.debug(f"DB에서 '{prompt_type}' 프롬프트 조회 성공")
                return prompt_text
            else:
                self.logger.debug(f"DB에서 '{prompt_type}' 프롬프트 찾을 수 없음. 빈 문자열 반환.")
                return "" # 프롬프트를 찾지 못한 경우 빈 문자열 반환
        except Exception as e:
            self.logger.error(f"DB에서 프롬프트 조회 중 오류 - Type: {prompt_type}, User: {user_id}: {e}", exc_info=True)
            raise # 예외를 다시 발생시켜 호출부에서 처리하도록 함

    async def initialize(self):
        """클라이언트를 초기화합니다. (프롬프트 로딩 제외)"""
        try:
            self.logger.info("클라이언트 초기화 시작 (프롬프트 로딩 제외)")
            
            await self.load_server_map()
            self.logger.debug(f"로드된 서버 맵: {json.dumps(self.server_map, ensure_ascii=False, indent=2)}")
            
            if not self.server_map:
                self.logger.warning("활성화된 서버가 없습니다. 도구 초기화는 건너뜁니다.")
            else:
                await self.initialize_tools()
                self.logger.debug(f"초기화된 도구 목록: {json.dumps(self.registered_tools, ensure_ascii=False, indent=2)}")
                if not self.registered_tools:
                    self.logger.warning("등록된 도구가 없습니다.")
            
            self.logger.info(f"클라이언트 초기화 완료 - 서버 {len(self.server_map)}개, 도구 {len(self.registered_tools)}개")
            return self
            
        except Exception as e:
            self.logger.error(f"클라이언트 초기화 실패: {str(e)}\\n{traceback.format_exc()}")
            raise

    async def load_server_map(self):
        """서버 맵을 로드합니다."""
        try:
            self.logger.info("서버 맵 로드 시작")
            server_data = await load_server_list()
            
            self.logger.debug(f"받은 서버 데이터: {json.dumps(server_data, ensure_ascii=False, indent=2)}")
            
            if not server_data:
                self.logger.error("서버 목록이 비어있습니다.")
                raise ValueError("서버 목록이 비어있습니다.")
            
            if isinstance(server_data, dict):
                server_list = server_data.get("servers", [])
            elif isinstance(server_data, list):
                server_list = server_data
            else:
                error_msg = f"잘못된 서버 목록 형식: {type(server_data)}"
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            if not server_list:
                error_msg = "서버 목록이 비어있습니다."
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            self.server_map.clear()  # 기존 서버 맵 초기화
            
            for server in server_list:
                if not isinstance(server, dict):
                    self.logger.warning(f"잘못된 서버 정보 형식: {server}")
                    continue
                    
                name = server.get("name", "이름 없음")
                url = server.get("url")
                enabled = server.get("enabled", True)
                
                if not url:
                    self.logger.warning(f"URL이 없는 서버 무시됨: {name}")
                    continue
                
                if not enabled:
                    self.logger.info(f"비활성화된 서버 무시됨: {name} ({url})")
                    continue
                
                self.server_map[url] = server
                self.logger.info(f"서버 추가됨: {name} ({url})")
            
            if not self.server_map:
                error_msg = "활성화된 서버가 없습니다."
                self.logger.error(error_msg)
                raise ValueError(error_msg)
            
            self.logger.info(f"서버 맵 로드 완료. 총 {len(self.server_map)}개의 서버가 활성화됨")
                
        except Exception as e:
            self.logger.error(f"서버 맵 로드 실패: {str(e)}\n{traceback.format_exc()}")
            raise

    async def initialize_tools(self) -> None:
        """서버에서 도구 목록을 초기화합니다."""
        self.logger.info("도구 목록 초기화 시작")
        
        if not self.server_map:
            self.logger.warning("활성화된 서버가 없습니다.")
            return
        
        # 기존 도구 목록 초기화
        self.registered_tools.clear()
        
        for server_url in self.server_map:
            self.logger.info(f"서버 {server_url}에서 도구 목록 조회 중...")
            try:
                result = await get_registered_tools(server_url)
                
                if "error" in result:
                    self.logger.warning(f"서버 {server_url}에서 도구 목록 조회 실패: {result['error']}")
                    continue
                    
                tools = result.get("tools", [])
                if not tools:
                    self.logger.warning(f"서버 {server_url}에서 도구를 찾을 수 없습니다.")
                    continue
                
                self.logger.info(f"서버 {server_url}에서 {len(tools)}개의 도구를 찾았습니다.")
                
                # 서버별 도구 목록 저장
                if server_url not in self.registered_tools:
                    self.registered_tools[server_url] = []
                
                for tool in tools:
                    tool_name = tool.get("name")
                    if not tool_name:
                        self.logger.warning("이름이 없는 도구를 무시합니다.")
                        continue
                    
                    self.logger.debug(f"도구 등록: {tool_name} - {tool.get('description', '설명 없음')}")
                    self.registered_tools[server_url].append(tool)
                
            except Exception as e:
                self.logger.error(f"서버 {server_url} 도구 목록 초기화 중 오류: {str(e)}")
                continue
        
        total_tools = sum(len(tools) for tools in self.registered_tools.values())
        if total_tools == 0:
            self.logger.warning("등록된 도구가 없습니다.")
        else:
            self.logger.info(f"총 {total_tools}개의 도구가 등록되었습니다.")
            self.logger.debug(f"등록된 도구 목록: {json.dumps(self.registered_tools, ensure_ascii=False, indent=2)}")

    async def call_tool(self, server_url: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """도구를 호출합니다."""
        try:
            self.logger.info(f"도구 호출 시작 - 서버: {server_url}, 도구: {tool_name}")
            self.logger.debug(f"호출 인자: {json.dumps(arguments, ensure_ascii=False)}")
            
            if server_url not in self.registered_tools:
                error_msg = f"서버 {server_url}에 등록된 도구가 없습니다."
                self.logger.error(error_msg)
                return {"error": error_msg}
            
            server_tools = self.registered_tools[server_url]
            if not any(tool.get("name") == tool_name for tool in server_tools):
                error_msg = f"도구 {tool_name}을(를) 찾을 수 없습니다."
                self.logger.error(error_msg)
                return {"error": error_msg}
            
            result = await call_mcp_tool(server_url, tool_name, arguments)
            self.logger.debug(f"도구 호출 결과: {json.dumps(result, ensure_ascii=False)}")
            
            if not result:
                error_msg = "도구 호출 결과가 없습니다."
                self.logger.error(error_msg)
                return {"error": error_msg}
            
            return result
            
        except Exception as e:
            error_msg = f"도구 호출 실패: {str(e)}"
            self.logger.error(f"{error_msg}\n{traceback.format_exc()}")
            return {"error": error_msg}

    async def call_tool_chain(self, tool_chain: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """순차적으로 도구를 호출합니다."""
        results = []
        
        for step in tool_chain:
            tool_name = step.get("tool_name")
            server_url = step.get("server_url")
            parameters = step.get("parameters", {})
            
            self.logger.info(f"도구 체인 단계 실행 - 도구: {tool_name}, 서버: {server_url}")
            self.logger.debug(f"단계 파라미터: {json.dumps(parameters, ensure_ascii=False)}")
            
            try:
                result = await self.call_tool(server_url, tool_name, parameters)
                results.append({
                    "tool_name": tool_name,
                    "success": "error" not in result,
                    "result": result
                })
                
                if "error" in result:
                    self.logger.error(f"도구 체인 실행 중단 - {tool_name} 실패: {result['error']}")
                    break
                    
            except Exception as e:
                error_msg = f"도구 체인 실행 중 오류: {str(e)}"
                self.logger.error(f"{error_msg}\n{traceback.format_exc()}")
                results.append({
                    "tool_name": tool_name,
                    "success": False,
                    "error": str(e)
                })
                break
        
        return results

    async def format_response_with_llm(self, db: AsyncSession, user_id: str, company_code: int, raw_response: str) -> Dict[str, Any]:
        """OpenAI API를 사용하여 서버 응답을 자연스러운 형태로 변환합니다."""
        try:
            # 저장된 프롬프트 가져오기 (DB, 사용자 컨텍스트 전달)
            system_prompt = await self.get_prompt(db, user_id, company_code, "format_response_with_llm")
            self.logger.debug(f"'format_response_with_llm' 프롬프트 : {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")
            if not system_prompt:
                # 기본 프롬프트 사용
                system_prompt = """당신은 MCP 서버의 응답을 자연스러운 한국어로 변환하는 전문가입니다.
응답은 친절하고 전문적이어야 합니다. 만약 응답값이 너무 클 경우 적당한 사이즈로 조절하여 전달해 주세요.
오류내용은 상태와 세부사항에 대한 내용만 전달하고, 오류 처리 방법은 제시하지 않습니다. 데이터의 형태에 따라 시계열 차트 또는 테이블로 변환해 주세요.
1. 시계열 차트 변환  
   - 응답에 `[{ "timestamp": X, "value": Y }, …]` 형태의 리스트가 있으면  
     - X축: `timestamp` (시간축)  
     - Y축: `value` (값)  
     시계열 차트로 변환하여 보여주세요.
   - 차트 변환 데이터는 번역하지 않습니다.
2. 테이블 변환  
   - 응답에 `List[Dict]` 형태의 반복 행(row)이 있으면 Markdown 표로 자동 변환합니다.  
   - 표의 헤더는 dict 키, 각 행은 dict 값으로 구성합니다.
   - 테이블 변환 데이터는 번역하지 않습니다.
오류내용은 상태와 세부사항에 대한 내용만 전달하고, 오류 처리 방법은 제시하지 않습니다."""


            self.logger.info("서버 응답을 자연스러운 형태로 변환합니다.")

            self.logger.debug(f"'format_response_with_llm' 프롬프트 처리 후: {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")
            
            # 필요한 파라미터를 감지하는 부분 추가
            parameter_required = False
            required_params = []
            tool_name_for_param_search = None # 변수 이름 명확화
            
            # 파싱된 JSON 응답에서 필요한 파라미터를 찾음
            try:
                response_json = json.loads(raw_response)
                if isinstance(response_json, dict):
                    if response_json.get("status") == "parameter_required":
                        parameter_required = True
                        required_params = response_json.get("required_params", [])
                        tool_name_for_param_search = response_json.get("tool_name") # 여기서 도구 이름 가져옴
                    # content 필드가 있는 경우 (중첩된 JSON)
                    elif response_json.get("content") and isinstance(response_json.get("content"), list):
                        for item in response_json.get("content"):
                            if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                                try:
                                    inner_json = json.loads(item.get("text"))
                                    if inner_json.get("status") == "parameter_required":
                                        parameter_required = True
                                        required_params = inner_json.get("required_params", [])
                                        tool_name_for_param_search = inner_json.get("tool_name") # 여기서 도구 이름 가져옴
                                except (json.JSONDecodeError, TypeError):
                                    pass
            except (json.JSONDecodeError, TypeError):
                self.logger.debug("원시 응답을 JSON으로 파싱할 수 없습니다")
            
            # LLM 요청 메시지 구성
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"다음 MCP 서버 응답을 자연스러운 한국어로 변환해 주세요:\n\n{raw_response}"}
            ]
            
            # 전송되는 전체 메시지 로깅
            self.logger.info("=== LLM 형식 변환 요청 메시지 ===")
            for idx, msg in enumerate(messages):
                self.logger.info(f"[{idx}] 역할: {msg['role']}")
                self.logger.info(f"[{idx}] 내용: {msg['content'][:1000]}{'...' if len(msg['content']) > 1000 else ''}")
                
            # API 호출
            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.5
            )
            
            formatted_text = response.choices[0].message.content
            
            # LLM 응답 상세 로깅
            self.logger.info("=== LLM 형식 변환 응답 ===")
            self.logger.info(f"모델: {response.model}")
            self.logger.info(f"토큰 사용: 입력={response.usage.prompt_tokens}, 출력={response.usage.completion_tokens}, 총={response.usage.total_tokens}")
            self.logger.info(f"응답 내용: {formatted_text[:1000]}{'...' if len(formatted_text) > 1000 else ''}")
            
            self.logger.info("응답 변환 완료")
            
            if parameter_required and required_params and tool_name_for_param_search: # tool_name_for_param_search 사용
                self.logger.info(f"필요한 파라미터 감지됨: {required_params}, 원래 요청 도구: {tool_name_for_param_search}")
                
                # 자동으로 적절한 도구 찾아 호출 (DB, 사용자 컨텍스트 전달)
                additional_result = await self.find_and_call_parameter_tool(db, user_id, company_code, required_params)
                
                if additional_result:
                    self.logger.info(f"LLM 재처리 시작 - 원본 LLM 응답 ({len(formatted_text)}자)과 추가 도구 결과 결합")
                    
                    # LLM 재처리를 위한 새 사용자 메시지 구성
                    combined_user_message_content = f"이전에 서버 응답을 다음과 같이 정리했습니다:\n-----\n{formatted_text}\n-----\n"

                    if additional_result.get("needs_user_input") and additional_result.get("message"):
                        combined_user_message_content += f"그런데, 사용자에게 다음 정보를 추가로 요청해야 합니다:\n-----\n{additional_result.get('message')}\n-----\n"
                        combined_user_message_content += "위 두 내용을 자연스럽게 결합하여 사용자에게 전달할 최종 응답을 작성해주세요. 먼저 정리된 서버 응답 내용을 전달하고, 그 후에 추가 정보 요청을 부드럽게 이어주세요."
                    elif additional_result.get("result") and additional_result.get("tool_name"):
                        tool_name = additional_result.get("tool_name", "알 수 없는 도구")
                        tool_output_str = json.dumps(additional_result.get("result"), ensure_ascii=False, indent=2)
                        combined_user_message_content += f"또한, 누락된 정보를 찾기 위해 '{tool_name}' 도구를 호출했고, 그 결과는 다음과 같습니다:\n-----\n{tool_output_str}\n-----\n"
                        combined_user_message_content += "이전에 정리된 서버 응답 내용과 방금 얻은 도구 결과를 자연스럽게 통합하여 최종 사용자 응답을 작성해주세요. 도구 결과가 사용자 질문에 대한 답변을 완성하거나 보충하는 역할을 할 수 있도록 내용을 구성해주세요."
                    else:
                        self.logger.warning(f"additional_result가 예상치 못한 구조를 가지고 있어 LLM 재처리를 건너뛰고 이전 결과와 함께 반환합니다: {additional_result}")
                        # 기존 방식대로 분리된 정보 반환 또는 formatted_text만 반환 결정 필요
                        # 여기서는 기존처럼 additional_result와 함께 반환
                        return {
                            "type": "text",
                            "text": formatted_text,
                            "additional_result": additional_result # LLM 재처리 실패 시 fallback
                        }

                    # 기존 system_prompt (format_response_with_llm 용) 재사용
                    messages_for_reprocessing = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": combined_user_message_content}
                    ]

                    self.logger.info("=== LLM 통합 재처리 요청 메시지 ===")
                    for idx, msg in enumerate(messages_for_reprocessing):
                        self.logger.info(f"[{idx}] 역할: {msg['role']}")
                        self.logger.info(f"[{idx}] 내용: {msg['content'][:1000]}{'...' if len(msg['content']) > 1000 else ''}")

                    # API 재호출
                    reprocessed_response_obj = await self.client.chat.completions.create(
                        model="gpt-4o", # 또는 적절한 모델
                        messages=messages_for_reprocessing,
                        temperature=0.5 
                    )

                    final_formatted_text = reprocessed_response_obj.choices[0].message.content

                    self.logger.info("=== LLM 통합 재처리 응답 ===")
                    self.logger.info(f"모델: {reprocessed_response_obj.model}")
                    if hasattr(reprocessed_response_obj, 'usage') and reprocessed_response_obj.usage:
                        self.logger.info(f"토큰 사용 (재처리): 입력={reprocessed_response_obj.usage.prompt_tokens}, 출력={reprocessed_response_obj.usage.completion_tokens}, 총={reprocessed_response_obj.usage.total_tokens}")
                    self.logger.info(f"최종 통합 응답 내용 ({len(final_formatted_text)}자): {final_formatted_text[:1000]}{'...' if len(final_formatted_text) > 1000 else ''}")
                    
                    return {
                        "type": "text",
                        "text": final_formatted_text,
                        "original_formatted_text": formatted_text, # 디버깅용
                        "additional_info_used": additional_result # 디버깅용 
                    }
            
            # additional_result가 없거나, 위의 if additional_result 블록에서 처리되지 않은 경우 (예: LLM 재처리 건너뛴 경우)
            # 또는 파라미터가 필요하지 않은 경우, 원래의 formatted_text 반환
            return {
                "type": "text",
                "text": formatted_text
            }
            
        except Exception as e:
            self.logger.error(f"응답 변환 중 오류 발생: {str(e)}\n{traceback.format_exc()}")
            # 오류 발생 시 원본 응답 반환
            return {
                "type": "text",
                "text": raw_response
            }

    # LLM에게 필요한 파라미터를 찾을 수 있는 도구 추천 요청
    async def find_and_call_parameter_tool(self, db: AsyncSession, user_id: str, company_code: int, required_params: List[str]) -> Dict[str, Any]:
        """필요한 파라미터를 찾기 위한 적절한 도구를 찾아 호출합니다."""
        
        self.logger.info(f"필요한 파라미터를 찾기 위한 도구 자동 호출 시작: {required_params} (User: {user_id}, Company: {company_code})")
        
        # LLM을 활용하여 적절한 도구 찾기
        tools_description = []
        for server_url, tools in self.registered_tools.items():
            for tool in tools:
                tools_description.append({
                    "name": tool.get("name"),
                    "description": tool.get("description", "설명 없음"),
                    "parameters": tool.get("inputSchema", {}).get("properties", {}),
                    "server_url": server_url
                })
        
        if not tools_description:
            self.logger.warning("등록된 도구가 없습니다")
            return {}
        
        
        # 저장된 프롬프트 가져오기 (DB, 사용자 컨텍스트 전달)
        system_prompt = await self.get_prompt(db, user_id, company_code, "find_and_call_parameter_tool")
        self.logger.debug(f"'find_and_call_parameter_tool' 프롬프트 : {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")
        if not system_prompt:
            # 기본 프롬프트 사용
            system_prompt = """당신은 필요한 파라미터를 찾기 위해 적절한 도구를 추천하는 AI 도우미입니다.
사용자의 요구사항을 주의 깊고 정확하게 따르고, 먼저 단계별로 생각하고, 무엇을 구축할지에 대한 계획을 의사코드로 상세히 확인하고 진행하세요.
1. 가능하다면 우선적으로 대화 기록에 있는 파라미터를 참고하세요.
2. 주어진 도구 목록의 입력 파라미터가 아닌 리턴값에서 필요한 파라미터를 찾아서 해당 파라미터 정보를 제공할 수 있는 가장 적합한 도구를 선택하세요.
3. 도구 선택 시, 도구의 기능과 파라미터를 정확히 이해하고, 필요한 모든 파라미터 값을 적절히 설정하세요.
4. 응답 값에 주석은 포함하지 마세요.
5. 만약 적절한 도구가 확인되지 않는다면, 사용자에게 파라미터를 직접 입력하도록 요청하세요.
5. 반드시 JSON 형식으로 응답하세요."""

        self.logger.debug(f"'find_and_call_parameter_tool' 프롬프트 처리 후: {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")

        try:
            messages = [
                {"role": "system", "content": system_prompt}, 
                {"role": "user", "content": f"""
필요한 파라미터: {json.dumps(required_params, ensure_ascii=False)}

사용 가능한 도구 목록:
{json.dumps(tools_description, ensure_ascii=False, indent=2)}

다음 형식으로 응답해주세요:
{{
    "tool_name": "선택한 도구 이름",
    "server_url": "도구가 있는 서버 URL",
    "explanation": "이 도구를 선택한 이유와 어떻게 필요한 파라미터를 제공할 수 있는지 설명",
    "parameters": {{}} // 도구 호출에 필요한 파라미터 (없으면 빈 객체)
}}
"""}
            ]
            
            self.logger.info("LLM에게 적절한 도구 추천 요청")
            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.2
            )
            
            content = response.choices[0].message.content
            self.logger.info(f"LLM 응답: {content}")
            
            # 응답에서 JSON 부분만 추출
            content = content.strip()
            if content.startswith('```json'):
                content = content.split('```json', 1)[1]
                if '```' in content:
                    content = content.split('```', 1)[0]
            elif content.startswith('```'):
                content = content.split('```', 1)[1]
                if '```' in content:
                    content = content.split('```', 1)[0]
            
            # JSON 파싱
            try:
                parsed = json.loads(content.strip())
                self.logger.info(f"LLM이 추천한 도구: {parsed.get('tool_name')}, 서버: {parsed.get('server_url')}")
                
                tool_name = parsed.get("tool_name")
                server_url = parsed.get("server_url")
                parameters = parsed.get("parameters", {})
                
                if not tool_name or not server_url:
                    self.logger.warning("LLM이 유효한 도구를 추천하지 않았습니다")
                    
                    # 사용자에게 파라미터 요청
                    return {
                        "needs_user_input": True,
                        "required_params": required_params,
                        "message": f"필요한 파라미터({ ', '.join(required_params) })를 찾을 수 있는 도구를 찾지 못했습니다. 직접 입력해 주세요."
                    }
                
                # 추천된 도구 호출
                self.logger.info(f"LLM이 추천한 도구 호출: {tool_name}, 서버: {server_url}")
                result = await self.call_tool(server_url, tool_name, parameters)
                
                if result.get("error"):
                    self.logger.warning(f"도구 호출 실패: {result.get('error')}")
                    return {
                        "needs_user_input": True,
                        "required_params": required_params,
                        "message": f"파라미터({ ', '.join(required_params) })를 찾기 위한 도구 호출에 실패했습니다. 직접 입력해 주세요.",
                        "error": result.get("error")
                    }
                
                # 도구 호출 결과를 반환 (파라미터 추출 없이)
                self.logger.info("도구 호출 결과를 반환합니다 (사용자가 직접 파라미터를 확인)")
                return {
                    "tool_name": tool_name,
                    "server_url": server_url,
                    "result": result
                }
                
            except json.JSONDecodeError:
                self.logger.warning(f"LLM 응답 파싱 실패. 원본 응답을 텍스트로 반환: {content}")
                return {
                    "needs_user_input": True,
                    "required_params": required_params,
                    "message": f"LLM 응답 파싱에 실패했습니다. 필요한 파라미터({ ', '.join(required_params) })를 직접 입력해 주세요.",
                    "error": str(e)
                }
                
        except Exception as e:
            self.logger.error(f"LLM을 활용한 도구 찾기 실패: {str(e)}\n{traceback.format_exc()}")
            return {
                "needs_user_input": True,
                "required_params": required_params,
                "message": f"파라미터({ ', '.join(required_params) })를 찾는 중 오류가 발생했습니다. 직접 입력해 주세요.",
                "error": str(e)
            }

    # 현재는 미사용, 필요시 DB, user_id, company_code 인자 추가 필요
    async def extract_params_from_result(self, db: AsyncSession, user_id: str, company_code: int, result: Dict[str, Any], required_params: List[str], tool_name: str) -> Dict[str, Any]:
        """도구 호출 결과에서 필요한 파라미터 값을 추출합니다."""
        
        self.logger.info(f"도구 호출 결과에서 필요한 파라미터 추출 시작: {required_params} (User: {user_id}, Company: {company_code})")
        
        # LLM을 활용하여 결과에서 파라미터 추출
        # get_prompt 호출 시 db, user_id, company_code 전달
        system_prompt = await self.get_prompt(db, user_id, company_code, "extract_params_from_tool_result") # 프롬프트 타입 예시
        self.logger.debug(f"'extract_params_from_tool_result' 프롬프트 : {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")
        if not system_prompt:
            system_prompt = """당신은 API 호출 결과에서 필요한 파라미터 값을 추출하는 전문가입니다.
주어진 결과 데이터를 분석하여 필요한 파라미터 값을 정확하게 추출하세요.
파라미터 이름과 유사하거나 관련된 필드를 찾아 매핑하세요.
데이터 구조를 이해하고 중첩된 객체나 배열에서도 값을 추출할 수 있어야 합니다.
반드시 JSON 형식으로 응답하세요.
"""
        self.logger.debug(f"'extract_params_from_tool_result' 프롬프트 처리 후: {json.dumps(system_prompt, ensure_ascii=False, indent=2)}")
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"""
도구 호출 결과:
{json.dumps(result, ensure_ascii=False, indent=2)}

필요한 파라미터: {json.dumps(required_params, ensure_ascii=False)}
도구 이름: {tool_name}

응답 형식:
{{
    "파라미터이름1": "추출한 값1",
    "파라미터이름2": "추출한 값2",
    ...
}}

추출할 수 없는 파라미터는 포함하지 마세요.
결과 데이터에서 각 파라미터에 해당하는 값을 찾아 추출하세요.
회사 정보의 경우 첫 번째 항목이나 가장 적합한 항목을 선택하세요.
"""}
            ]
            
            self.logger.info("LLM에게 파라미터 추출 요청")
            response = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.2
            )
            
            content = response.choices[0].message.content
            self.logger.debug(f"LLM 파라미터 추출 응답: {content}")
            
            # 응답에서 JSON 부분만 추출
            content = content.strip()
            if content.startswith('```json'):
                content = content.split('```json', 1)[1]
                if '```' in content:
                    content = content.split('```', 1)[0]
            elif content.startswith('```'):
                content = content.split('```', 1)[1]
                if '```' in content:
                    content = content.split('```', 1)[0]
            
            # JSON 파싱
            extracted_params = json.loads(content.strip())
            self.logger.info(f"추출된 파라미터: {extracted_params}")
            
            # 모든 필요한 파라미터가 추출되었는지 확인
            found_params = set(extracted_params.keys())
            required_params_set = set(required_params)
            
            if found_params.intersection(required_params_set):
                self.logger.info(f"필요한 파라미터 중 {len(found_params.intersection(required_params_set))}개가 추출됨")
                return extracted_params
            else:
                self.logger.warning("필요한 파라미터가 추출되지 않았습니다")
                
                # 직접 추출 시도 (기본 규칙 기반 추출)
                direct_extracted = {}
                
                # 회사 정보 추출
                if "companies" in result and isinstance(result["companies"], list) and result["companies"]:
                    first_company = result["companies"][0]
                    if "companyCode" in required_params and "code" in first_company:
                        direct_extracted["companyCode"] = first_company["code"]
                    if "companyName" in required_params and "name" in first_company:
                        direct_extracted["companyName"] = first_company["name"]
                
                if direct_extracted:
                    self.logger.info(f"직접 추출한 파라미터: {direct_extracted}")
                    return direct_extracted
                    
                return {}
                
        except Exception as e:
            self.logger.error(f"파라미터 추출 실패: {str(e)}\n{traceback.format_exc()}")
            return {}

    async def process_user_input(self, db: AsyncSession, user_id: str, company_code: int, user_input: str, chat_history: List[Dict[str, str]] = None, image_data: Optional[str] = None) -> Dict[str, Any]:
        try:
            self.logger.info(f"사용자 입력 처리 시작 (도구 로직): {user_input[:50]} (User: {user_id}, Image Present: {True if image_data else False})")
            self.add_to_history("user", user_input)

            if not self.registered_tools:
                self.logger.warning("등록된 도구가 없습니다. 다시 초기화 시도.")
                await self.initialize_tools()
            
            if not self.registered_tools:
                error_msg = "사용 가능한 도구가 없습니다."
                self.logger.error(error_msg)
                self.add_to_history("assistant", error_msg)
                return {"type": "text", "text": error_msg}

            tools_description_for_llm = []
            for server_url_key, tools_in_server in self.registered_tools.items():
                for tool_item in tools_in_server:
                    tools_description_for_llm.append({
                        "name": tool_item.get("name"),
                        "description": tool_item.get("description", "설명 없음"),
                        "parameters": tool_item.get("inputSchema", {}).get("properties", {}),
                        "server_url": server_url_key
                    })
            
            self.logger.debug(f"LLM 전달용 도구 목록: {json.dumps(tools_description_for_llm, ensure_ascii=False, indent=2)}")

            system_prompt_for_tool_selection = await self.get_prompt(db, user_id, company_code, "process_user_input")
            
            json_response_format_example = '''{
    "tool_name": "선택한 도구 이름 또는 null (적절한 도구가 없거나 일반 응답이 필요할 경우)",
    "server_url": "도구가 있는 서버 URL 또는 null",
    "parameters": {
        "파라미터이름": "사용자 요청 및 대화 기록, (제공된 경우)이미지 내용에서 추출한 값" 
    },
    "missing_parameters": ["아직 값이 없는 필수 파라미터 목록 (없으면 빈 배열 [])"],
    "parameter_source_hint": {
        "파라미터이름": "해당 파라미터를 얻을 수 있는 방법에 대한 힌트 (예: '사용자에게 직접 질문', '다른 특정 도구 이름', '대화 기록 또는 이미지 내용에서 찾아보세요' 등)"
    },
    "requires_further_action": true 또는 false,
    "response_for_user": "만약 tool_name이 null이거나 즉시 사용자에게 응답해야 할 경우 여기에 메시지 작성"
}'''

            messages_for_tool_selection = [
                {"role": "system", "content": system_prompt_for_tool_selection}
            ]
            if chat_history:
                messages_for_tool_selection.extend(chat_history)
            
            # --- 사용자 메시지 content 구성 (이미지 데이터 통합) ---
            user_message_content_parts = []

            # 사용자 요청 텍스트 부분
            # 긴 지시사항 JSON 문자열을 생성합니다.
            # f-string 내에서 백슬래시와 중괄호를 주의하여 처리합니다.
            tools_json_str = json.dumps(tools_description_for_llm, ensure_ascii=False, indent=2)
            chat_history_json_str = json.dumps(chat_history[-5:], ensure_ascii=False, indent=2) if chat_history else "없음"

            user_input_text_content = (
                f"사용 가능한 도구 목록:\\n{tools_json_str}\\n\\n"
                f"사용자 요청: {user_input}\\n\\n"
                f"대화 기록 (필요시 참고):\\n{chat_history_json_str}\\n\\n"
                f"반드시 다음 형식으로만 응답해주세요:\\n{json_response_format_example}\\n\\n"
                "지침:\\n"
                "1. 사용자 요청, (제공된 경우) 첨부된 이미지, 그리고 대화 기록을 분석하여, 요청을 가장 잘 처리할 수 있는 도구를 '사용 가능한 도구 목록'에서 선택하세요.\\n"
                "2. 이미지가 제공된 경우, 이미지의 내용을 분석하여 도구 선택 및 파라미터 추출에 활용하세요. 예를 들어, 이미지에 특정 제품이 있다면 해당 제품명을 검색 도구의 파라미터로 사용할 수 있습니다.\\n"
                "3. 선택한 도구 실행에 필요한 모든 파라미터를 식별하세요.\\n"
                "4. 사용자 요청, 대화 기록, (제공된 경우)이미지 내용에서 해당 파라미터 값들을 최대한 추출하여 'parameters' 필드에 채우세요.\\n"
                "5. 만약 필수 파라미터 값이 부족하여 도구를 즉시 실행할 수 없다면, 'missing_parameters' 배열에 해당 파라미터 이름들을 명시하고, 'requires_further_action'을 true로 설정하세요.\\n"
                "6. 'parameter_source_hint'에는 각 'missing_parameters'를 어떻게 얻을 수 있을지에 대한 당신의 추론을 간략히 적어주세요. (예: 사용자에게 직접 물어봐야 할지, 다른 도구를 사용해야 할지, 이미지에서 정보를 더 찾아야할지 등)\\n"
                "7. 만약 적절한 도구가 없거나, 사용자에게 직접 응답하는 것이 더 적절하다고 판단되면, 'tool_name'을 null로 설정하고 'response_for_user' 필드에 사용자에게 전달할 메시지를 작성하세요. 이 경우 'requires_further_action'은 false가 됩니다.\\n"
                "8. 도구를 선택했고 모든 파라미터 값이 준비되었다면, 'missing_parameters'는 빈 배열, 'requires_further_action'은 false로 설정하세요.\\n\\n"
                "절대로 추가 설명이나 마크다운 형식을 사용하지 마세요. 응답 앞뒤에 코드 블록(```)을 포함하지 마세요.\\n"
                "순수한 JSON 객체만 반환하세요."
            )
            user_message_content_parts.append({"type": "text", "text": user_input_text_content})

            if image_data:
                if image_data.startswith("data:image"):
                    user_message_content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": image_data, "detail": "auto"} 
                    })
                    self.logger.info("이미지 데이터를 LLM 요청에 포함합니다.")
                    # 이미지 포함 시 LLM에게 이미지 분석을 더 명확히 지시하기 위해 프롬프트에 텍스트 추가 가능
                    user_message_content_parts[0]["text"] = "(첨부된 이미지를 참고하여 답변해주세요)\\n\\n" + user_message_content_parts[0]["text"]
                else:
                    self.logger.warning(f"제공된 image_data가 유효한 Data URL 형식이 아닙니다. 텍스트 요청으로만 진행합니다: {image_data[:100]}...")
            
            messages_for_tool_selection.append({"role": "user", "content": user_message_content_parts})
            # --- 사용자 메시지 content 구성 완료 ---

            self.logger.info("LLM 도구 선택 요청 시작", extra={'log_type': 'llm'})
            # 요청 페이로드 로깅 (민감 정보 주의)
            self.logger.debug(f"LLM Request Payload: {json.dumps(messages_for_tool_selection, ensure_ascii=False, indent=2)}")

            llm_tool_response = await self.client.chat.completions.create(
                model='gpt-4o',
                messages=messages_for_tool_selection,
                temperature=0.2
            )
            self.logger.info("LLM 도구 선택 응답 수신", extra={'log_type': 'llm'})
            
            raw_llm_content = llm_tool_response.choices[0].message.content
            self.logger.debug(f"LLM 도구 선택 원본 응답: {raw_llm_content}")

            try:
                processed_content = raw_llm_content.strip()
                if processed_content.startswith('```json'):
                    processed_content = processed_content[len('```json'):].strip()
                    if processed_content.endswith('```'):
                        processed_content = processed_content[:-len('```')].strip()
                elif processed_content.startswith('```'):
                    processed_content = processed_content[len('```'):].strip()
                    if processed_content.endswith('```'):
                        processed_content = processed_content[:-len('```')].strip()
                
                parsed_tool_call = json.loads(processed_content)
                tool_name_to_call = parsed_tool_call.get("tool_name")
                server_url_for_call = parsed_tool_call.get("server_url")
                params_for_call = parsed_tool_call.get("parameters", {})
                missing_parameters = parsed_tool_call.get("missing_parameters", [])
                requires_further_action = parsed_tool_call.get("requires_further_action", False)
                response_for_user = parsed_tool_call.get("response_for_user")
                parameter_source_hint = parsed_tool_call.get("parameter_source_hint", {})

                # Case 1: LLM이 직접 사용자 응답 생성
                if (not tool_name_to_call or not server_url_for_call) and response_for_user:
                    self.logger.info(f"LLM이 직접 응답 제공: {response_for_user}")
                    self.add_to_history("assistant", response_for_user)
                    return {"type": "text", "text": response_for_user} 
                
                # Case 2: 도구 실행 조건 충족 (모든 파라미터 준비 완료)
                if tool_name_to_call and server_url_for_call and not requires_further_action and not missing_parameters:
                    self.logger.info(f"LLM 선택 도구 실행 (파라미터 준비 완료): {tool_name_to_call}, 서버: {server_url_for_call}")
                    tool_call_result = await self.call_tool(server_url_for_call, tool_name_to_call, params_for_call)
                    
                    # format_response_with_llm 호출 시, 현재 이미지 데이터를 직접 전달하지 않음.
                    # 만약 도구 결과와 원본 이미지를 함께 LLM에 전달하여 최종 응답을 생성하고 싶다면,
                    # format_response_with_llm 시그니처 및 로직 수정 필요.
                    # 현재는 도구 결과(텍스트 예상)만 format_response_with_llm으로 넘김.
                    formatted_response = await self.format_response_with_llm(db, user_id, company_code, json.dumps(tool_call_result, ensure_ascii=False))
                    self.add_to_history("assistant", formatted_response.get("text", str(tool_call_result)))
                    return formatted_response
                
                # Case 3: 파라미터 수집 등 추가 작업 필요
                if tool_name_to_call and server_url_for_call and requires_further_action and missing_parameters:
                    self.logger.info(f"파라미터 수집 시작 - 주 도구: {tool_name_to_call}, 초기 누락: {missing_parameters}")
                    accumulated_parameters = params_for_call.copy()
                    max_iterations = 3 
                    asked_user_for_param = False 

                    for i in range(max_iterations):
                        current_missing_params = [p for p in missing_parameters if p not in accumulated_parameters]
                        if not current_missing_params:
                            self.logger.info(f"모든 파라미터 수집 완료: {accumulated_parameters}")
                            break 

                        param_to_find = current_missing_params[0]
                        self.logger.info(f"(시도 {i+1}/{max_iterations}) 다음 누락 파라미터 처리: {param_to_find}")

                        hint = parameter_source_hint.get(param_to_find, "")
                        should_re_evaluate_image = "이미지" in hint or "image" in hint.lower() 
                        
                        if should_re_evaluate_image and image_data:
                            # TODO: 이미지 재분석 또는 특정 이미지 관련 도구 호출 로직 (현재는 미구현)
                            # 이 경우, 다시 LLM에게 "이 이미지에서 '{param_to_find}' 정보를 추출해줘" 와 같은 요청을 보낼 수 있음.
                            # 또는, 이미지 분석 전용 도구가 있다면 해당 도구를 호출.
                            self.logger.info(f"'{param_to_find}' 정보를 이미지에서 찾아야 한다는 힌트 수신. (현재 이 로직은 미구현)")
                            # 일단은 find_and_call_parameter_tool로 넘어가도록 함.
                            # 실제 구현 시, 여기서 별도 LLM 호출이나 이미지 분석 도구 호출 후 continue 또는 break.

                        should_call_find_tool = "도구" in hint or "tool" in hint.lower() or should_re_evaluate_image # 이미지 힌트도 find_tool로 연결

                        if should_call_find_tool:
                            self.logger.info(f"'{param_to_find}' 해결 위해 find_and_call_parameter_tool 호출")
                            # find_and_call_parameter_tool 에도 image_data 컨텍스트 전달 여부 고려 필요
                            # 현재는 전달하지 않음. 필요시 시그니처 변경 및 내부 로직 수정.
                            aux_tool_search_result = await self.find_and_call_parameter_tool(db, user_id, company_code, [param_to_find])
                            # ... (기존 파라미터 수집 로직) ...
                            if aux_tool_search_result.get("needs_user_input"):
                                self.logger.info(f"find_and_call_parameter_tool 결과, 사용자 입력 필요: {aux_tool_search_result.get('message')}")
                                response_for_user = aux_tool_search_result.get("message")
                                asked_user_for_param = True
                                break 
                            
                            elif aux_tool_search_result.get("result"):
                                aux_tool_name = aux_tool_search_result.get("tool_name", "unknown_aux_tool")
                                # extract_params_from_result 에도 image_data 컨텍스트 전달 여부 고려 필요
                                extracted_params = await self.extract_params_from_result(
                                    db, user_id, company_code, 
                                    aux_tool_search_result["result"],
                                    [param_to_find], 
                                    aux_tool_name
                                )
                                if param_to_find in extracted_params:
                                    self.logger.info(f"'{param_to_find}' 값 추출 성공: {extracted_params[param_to_find]}")
                                    accumulated_parameters[param_to_find] = extracted_params[param_to_find]
                                else:
                                    self.logger.warning(f"'{param_to_find}' 값 추출 실패. 다음 파라미터/시도로 넘어감.")
                            else:
                                self.logger.warning(f"find_and_call_parameter_tool이 유효한 결과나 사용자 입력 요청을 반환하지 않음.")
                        else: 
                            self.logger.info(f"'{param_to_find}'에 대한 도구 힌트 없음 또는 사용자 질문 필요. 사용자에게 직접 질문 준비.")
                            pass 
                    
                    final_missing_params = [p for p in missing_parameters if p not in accumulated_parameters]
                    if not final_missing_params:
                        self.logger.info(f"모든 파라미터 수집 후 주 도구 실행: {tool_name_to_call}")
                        tool_call_result = await self.call_tool(server_url_for_call, tool_name_to_call, accumulated_parameters)
                        formatted_response = await self.format_response_with_llm(db, user_id, company_code, json.dumps(tool_call_result, ensure_ascii=False))
                        self.add_to_history("assistant", formatted_response.get("text", str(tool_call_result)))
                        return formatted_response
                    else:
                        if not asked_user_for_param: 
                            response_for_user = f"'{tool_name_to_call}' 도구 실행에 다음 정보가 더 필요합니다: {', '.join(final_missing_params)}. 알려주시겠어요?"
                        self.logger.info(f"파라미터 수집 실패 또는 사용자에게 질문 필요. 응답: {response_for_user}")
                        self.add_to_history("assistant", response_for_user)
                        return {"type": "text", "text": response_for_user}

                self.logger.warning(f"LLM 응답 처리 경로 불분명. LLM 원본 또는 일반 메시지 반환: {raw_llm_content}")
                fallback_message = response_for_user if response_for_user else raw_llm_content
                if not fallback_message or not fallback_message.strip():
                    fallback_message = "요청을 이해하지 못했습니다. 다시 시도해 주시겠어요?"
                self.add_to_history("assistant", fallback_message)
                return {"type": "text", "text": fallback_message}

            except json.JSONDecodeError:
                self.logger.warning(f"LLM 도구 선택 응답 파싱 실패. 원본 응답을 텍스트로 반환: {raw_llm_content}")
                self.add_to_history("assistant", raw_llm_content)
                return {"type": "text", "text": raw_llm_content}

        except Exception as e:
            self.logger.error(f"사용자 입력 처리 중 심각한 오류: {e}", exc_info=True)
            error_response = "죄송합니다, 요청을 처리하는 중 오류가 발생했습니다."
            self.add_to_history("assistant", error_response)
            return {"type": "text", "text": error_response}

    async def summarize_conversation(self, db: AsyncSession, user_id: str, company_code: int, history: List[Dict[str, str]]) -> Optional[str]:
        self.logger.info(f"대화 요약 시작 - User: {user_id}, Company: {company_code}, History: {len(history)} messages")
        if not self.client:
            self.logger.error("OpenAI 클라이언트가 초기화되지 않았습니다.")
            return None
        if not history:
            self.logger.warning("요약할 대화 내용이 없습니다.")
            return "요약할 대화 내용이 없습니다."

        # 요약 지시 프롬프트 가져오기 (예: 'summarize_instructions' 타입의 프롬프트)
        # 실제 프롬프트 타입은 프로젝트에 맞게 정의해야 함
        instruction_prompt_text = await self.get_prompt(db, user_id, company_code, "summarize_instructions")
        if not instruction_prompt_text:
            self.logger.warning("요약 지시 프롬프트를 찾을 수 없습니다. 기본 지시를 사용합니다.")
            instruction_prompt_text = "다음 대화 내용을 한국어로 간결하게 요약해 주세요:"
            # 또는 특정 기본 프롬프트를 하드코딩하거나, 오류 반환

        conversation_text = "\\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
        
        messages = [
            {"role": "system", "content": instruction_prompt_text},
            {"role": "user", "content": conversation_text}
        ]

        try:
            response = await self.client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.5,
                max_tokens=4096,
            )
            summary = response.choices[0].message.content.strip()
            self.logger.info(f"대화 요약 생성 완료 (User: {user_id}): {summary[:100]}...")
            return summary
        except Exception as e:
            self.logger.error(f"OpenAI API 호출 중 오류 (요약) - User: {user_id}: {str(e)}", exc_info=True)
            return None 