import asyncio
import json
import logging
import uuid
from typing import Dict, List, Any, Optional, Union
import httpx
import traceback

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("mcp_sse_client")

class McpSseClient:
    """MCP SSE 클라이언트 - SSE를 통한 MCP 서버 연결 및 도구 호출"""
    
    def __init__(self, server_url: str = "http://localhost:8000"):
        """
        MCP SSE 클라이언트 초기화
        
        Args:
            server_url: MCP 서버 URL
        """
        self.server_url = server_url.rstrip('/')
        self.session_id: Optional[str] = None
        self.messages_url: Optional[str] = None
        self.tools: List[Dict[str, Any]] = []
        self.initialized = False
        self.connected = False
        self.client = None
        self.sse_response = None
        
        # 이벤트 및 플래그
        self.init_complete = asyncio.Event()
        self.tools_received = asyncio.Event()
    
    def make_msg(self, method: str, params: dict, notify: bool=False) -> dict:
        """
        JSON-RPC 메시지 생성
        
        Args:
            method: 메서드 이름
            params: 파라미터 객체
            notify: 알림 여부 (ID 없음)
            
        Returns:
            JSON-RPC 메시지
        """
        msg: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if not notify:
            msg["id"] = str(uuid.uuid4())
        return msg
    
    async def post_message(self, client: httpx.AsyncClient, msg: dict) -> Optional[dict]:
        """
        메시지 전송
        
        Args:
            client: HTTPX 클라이언트
            msg: 전송할 메시지
            
        Returns:
            응답 데이터 또는 None
        """
        if not self.messages_url:
            logger.error("메시지 URL이 설정되지 않았습니다")
            return None
            
        full_url = f"{self.server_url}{self.messages_url}"
        logger.info(f"메시지 전송: {json.dumps(msg, ensure_ascii=False)}")
        
        try:
            response = await client.post(
                full_url, 
                json=msg,
                headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
            
            # 응답 처리
            if response.status_code == 200:
                try:
                    response_data = response.json()
                    logger.info(f"응답 수신: {json.dumps(response_data, ensure_ascii=False)}")
                    return response_data
                except json.JSONDecodeError:
                    logger.error(f"응답 JSON 파싱 실패: {response.text}")
                    logger.info(f"HTTP {response.status_code} BODY: {response.text}")
            else:
                logger.info(f"HTTP {response.status_code} BODY: {response.text}")
            
            return None
        except Exception as e:
            logger.error(f"메시지 전송 중 오류: {str(e)}")
            return None
    
    async def initialize_connection(self, client: httpx.AsyncClient) -> bool:
        """
        MCP 서버 연결 초기화
        
        Args:
            client: HTTPX 클라이언트
            
        Returns:
            초기화 성공 여부
        """
        init_msg = self.make_msg(
            "initialize",
            {
                "protocolVersion": "v1",
                "clientInfo": {
                    "name": "mcp_sse_client",
                    "version": "1.0.0"
                },
                "capabilities": {}
            }
        )
        
        logger.info("초기화 메시지 전송")
        init_response = await self.post_message(client, init_msg)
        
        return init_response is not None
    
    async def send_initialized_notification(self, client: httpx.AsyncClient) -> bool:
        """
        초기화 완료 알림 전송
        
        Args:
            client: HTTPX 클라이언트
            
        Returns:
            알림 전송 성공 여부
        """
        notify_msg = self.make_msg("notifications/initialized", {}, notify=True)
        logger.info("초기화 완료 알림 전송")
        
        try:
            await self.post_message(client, notify_msg)
            return True
        except Exception as e:
            logger.error(f"초기화 완료 알림 전송 실패: {str(e)}")
            return False
    
    async def list_tools(self, client: httpx.AsyncClient) -> List[Dict[str, Any]]:
        """
        도구 목록 조회
        
        Args:
            client: HTTPX 클라이언트
            
        Returns:
            도구 목록
        """
        if not self.initialized:
            logger.error("클라이언트가 초기화되지 않았습니다")
            return []
            
        # 도구 목록 요청 전 짧은 대기
        await asyncio.sleep(0.1)
        
        list_msg = self.make_msg("tools/list", {})
        logger.info(f"도구 목록 요청: {json.dumps(list_msg, ensure_ascii=False)}")
        
        response = await self.post_message(client, list_msg)
        
        if response and "result" in response and "tools" in response["result"]:
            tools = response["result"]["tools"]
            logger.info(f"도구 목록: {len(tools)} 개 도구 발견")
            if tools:
                logger.info(f"도구 목록 내용: {json.dumps(tools, ensure_ascii=False)}")
            for tool in tools:
                logger.info(f"- {tool['name']}: {tool['description']}")
                
            # 도구 목록 즉시 업데이트
            self.tools = tools
            
            # 도구 목록 수신 이벤트 설정
            if not self.tools_received.is_set():
                self.tools_received.set()
                
            return tools
        else:
            logger.error(f"도구 목록 조회 실패, 응답: {json.dumps(response, ensure_ascii=False) if response else 'None'}")
            return []
    
    async def connect(self) -> bool:
        """
        서버에 SSE 연결을 수립하고 초기화
        
        Returns:
            연결 성공 여부
        """

        logger.info(f"connect 호출: {self}")

        # 이미 연결되어 있는 경우 바로 성공 반환
        if self.connected and self.initialized:
            return True
            
        # 초기화 및 연결 플래그 리셋
        self.connected = False
        self.initialized = False
        self.init_complete.clear()
        self.tools_received.clear()
        
        # 결과 대기 이벤트 저장을 위한 딕셔너리
        self.pending_results = {}
        
        # 재연결 시도 횟수 및 연결 상태 추적
        self.reconnect_attempts = 0
        self.last_activity_time = asyncio.get_event_loop().time()
        
        sse_url = f"{self.server_url}/sse/"
        logger.info(f"SSE 연결 시도: {sse_url}")
        
        # 리스너 작업 참조를 위한 변수
        self.listener_task = None
        
        async def sse_listener():            
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=None) as client:
                    self.client = client  # 클라이언트 저장
                    
                    # SSE 스트림에 연결
                    async with client.stream('GET', sse_url, headers={"Accept": "text/event-stream"}) as response:
                        response.raise_for_status()
                        logger.info(f"SSE 연결 성공: {response.status_code}")
                        self.connected = True
                        self.sse_response = response  # 응답 객체 저장
                        
                        # 연결 상태 업데이트
                        self.reconnect_attempts = 0
                        self.last_activity_time = asyncio.get_event_loop().time()
                        
                        # SSE 이벤트 처리
                        expect_endpoint = False
                        did_init = False
                        
                        async for line in response.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                                
                            # 활동 시간 업데이트
                            self.last_activity_time = asyncio.get_event_loop().time()    
                                
                            # 1) event: ... 처리
                            if line.startswith("event:"):
                                event_type = line[6:].strip()
                                logger.info(f"이벤트 유형: {event_type}")
                                if event_type == "endpoint":
                                    expect_endpoint = True
                                continue
                                
                            # 2) data: ... 처리
                            elif line.startswith("data:"):
                                data = line[5:].strip()
                                logger.info(f"SSE 데이터: {data}")
                                
                                # 엔드포인트 정보 처리
                                if expect_endpoint:
                                    try:
                                        path, query = data.split("?", 1)
                                        self.messages_url = f"{path}?{query}"
                                        self.session_id = query.split("session_id=", 1)[1]
                                        logger.info(f"메시지 URL: {self.messages_url}")
                                        logger.info(f"세션 ID: {self.session_id}")
                                        
                                        # 초기화 시작
                                        await self.initialize_connection(client)
                                        
                                    except Exception as e:
                                        logger.error(f"엔드포인트 처리 중 오류: {str(e)}")
                                    
                                    expect_endpoint = False
                                    continue
                                
                                # JSON 메시지 처리
                                try:
                                    data_json = json.loads(data)
                                    logger.info(f"JSON 데이터: {json.dumps(data_json, ensure_ascii=False)}")
                                    
                                    # 초기화 응답 처리 (test_sse_2.py와 비슷하게)
                                    if (not did_init and data_json.get("id") and
                                        "result" in data_json and 
                                        "serverInfo" in data_json["result"]):
                                        server_info = data_json["result"]["serverInfo"]
                                        logger.info(f"서버 정보: {server_info.get('name', 'Unknown')} {server_info.get('version', 'Unknown')}")
                                        self.initialized = True
                                        did_init = True
                                        
                                        # 초기화 완료 알림 전송
                                        await asyncio.sleep(0.5)
                                        await self.send_initialized_notification(client)
                                        
                                        # 도구 목록 요청 (초기화 완료 후 약간 지연)
                                        await asyncio.sleep(0.5)
                                        await self.list_tools(client)
                                        
                                        # 초기화 완료 이벤트 설정
                                        self.init_complete.set()
                                        continue
                                    
                                    # 도구 목록 응답 처리 
                                    if (data_json.get("id") and 
                                        "result" in data_json and 
                                        "tools" in data_json["result"]):
                                        tools = data_json["result"]["tools"]
                                        logger.info(f"도구 목록 수신: {len(tools)}개")
                                        logger.info(f"도구 목록 내용: {json.dumps(tools, ensure_ascii=False)}")
                                        
                                        # 도구 목록 즉시 업데이트
                                        self.tools = tools
                                        
                                        # 도구 목록 수신 이벤트 설정
                                        if not self.tools_received.is_set():
                                            self.tools_received.set()
                                        continue
                                    
                                    # 도구 결과 처리 - ID 기반 응답 처리
                                    if data_json.get("id") and "result" in data_json:
                                        msg_id = data_json.get("id")
                                        if msg_id in self.pending_results:
                                            event = self.pending_results[msg_id]["event"]
                                            self.pending_results[msg_id]["result"] = data_json["result"]
                                            event.set()
                                            logger.info(f"도구 호출 결과 수신 완료: ID {msg_id}")
                                        continue
                                    
                                    # 도구 결과 처리
                                    if data_json.get("method") == "tools/result":
                                        logger.info(f"도구 결과 수신: {json.dumps(data_json['params'], ensure_ascii=False)}")
                                        continue
                                    
                                    # 알림 메시지 처리
                                    if data_json.get("method") == "notifications/initialized":
                                        logger.info("초기화 완료 알림 수신")
                                        
                                        # 이미 초기화되지 않은 경우
                                        if not self.initialized:
                                            self.initialized = True
                                            
                                            # 도구 목록 요청
                                            await asyncio.sleep(0.5)
                                            await self.list_tools(client)
                                            
                                            # 초기화 완료 이벤트 설정
                                            if not self.init_complete.is_set():
                                                self.init_complete.set()
                                        continue
                                
                                except json.JSONDecodeError:
                                    # SSE 서버가 보내는 초기 메시지 등은 JSON이 아닐 수 있음
                                    logger.warning(f"JSON 파싱 실패: {data}")
                                    pass
                                
                        # for 루프가 종료된 경우 - 정상적인 연결 종료 처리
                        logger.info("SSE 연결 종료됨 (정상)")
                        self.connected = False
                                    
            except Exception as e:
                logger.error(f"SSE 연결 오류: {str(e)}\n{traceback.format_exc()}")
                self.connected = False
                
                if not self.init_complete.is_set():
                    self.init_complete.set()  # 오류 시 이벤트 설정하여 대기 중인 작업 해제
                if not self.tools_received.is_set():
                    self.tools_received.set()
                
                # 대기 중인 모든 요청에 오류 전달
                for msg_id, data in self.pending_results.items():
                    if not data["event"].is_set():
                        data["result"] = {"error": f"SSE 연결 오류: {str(e)}"}
                        data["event"].set()
                        
                # 재연결 시도 (최대 5회까지)
                if self.reconnect_attempts < 5:
                    self.reconnect_attempts += 1
                    backoff_time = min(2 ** self.reconnect_attempts, 30)  # 지수 백오프: 2, 4, 8, 16, 30초
                    logger.info(f"SSE 재연결 시도 ({self.reconnect_attempts}/5): {backoff_time}초 후 재시도")
                    
                    # 다른 스레드에서 실행
                    reconnect_task = asyncio.create_task(self._delayed_reconnect(backoff_time))
            
        # 리스너 시작
        self.listener_task = asyncio.create_task(sse_listener())
        
        # Keepalive 작업 시작
        self.keepalive_task = asyncio.create_task(self._keepalive_loop())
        
        try:
            # 초기화 완료 대기 (최대 30초)
            await asyncio.wait_for(self.init_complete.wait(), 30.0)
            logger.info("초기화 완료")
            
            # 도구 목록 수신 대기 (최대 15초)
            await asyncio.wait_for(self.tools_received.wait(), 15.0)
            logger.info(f"도구 목록 수신 완료: {len(self.tools)}개")
            
            return self.initialized
            
        except asyncio.TimeoutError:
            logger.error("연결 타임아웃")
            return False
    
    async def _delayed_reconnect(self, delay: float):
        """지정된 지연 시간 후 SSE 재연결을 시도합니다."""
        try:
            await asyncio.sleep(delay)
            if not self.connected and not self.initialized:
                logger.info(f"{delay}초 후 SSE 재연결 시도 중...")
                await self.connect()
        except Exception as e:
            logger.error(f"재연결 시도 중 오류: {str(e)}")
    
    async def _keepalive_loop(self):
        """SSE 연결을 유지하기 위한 주기적인 ping 메시지 전송"""
        while True:
            try:
                await asyncio.sleep(20)  # 20초마다 확인
                
                # 연결 상태 확인
                if not self.connected or not self.client:
                    continue
                    
                # 마지막 활동 시간 확인 (60초 이상 활동이 없으면 ping 전송)
                current_time = asyncio.get_event_loop().time()
                if current_time - self.last_activity_time > 60:
                    try:
                        if self.initialized and self.client:
                            # MCP 프로토콜에 맞는 keepalive 메시지 사용
                            # 'ping' 대신 'tools/list' 요청을 사용하여 연결 유지
                            keepalive_msg = self.make_msg("tools/list", {})
                            await self.post_message(self.client, keepalive_msg)
                            self.last_activity_time = current_time
                            logger.debug("Keepalive 메시지 전송 (tools/list)")
                    except Exception as e:
                        logger.warning(f"Keepalive 메시지 전송 중 오류: {str(e)}")
                        
                        # 연결이 끊어진 경우 재연결 시도
                        if not self.connected:
                            logger.info("Keepalive 실패로 인한 재연결 시도")
                            reconnect_task = asyncio.create_task(self._delayed_reconnect(2.0))
                
            except asyncio.CancelledError:
                # 작업 취소 시 루프 종료
                logger.debug("Keepalive 작업 종료")
                break
            except Exception as e:
                logger.error(f"Keepalive 루프 오류: {str(e)}")
    
    async def get_tools(self) -> List[Dict[str, Any]]:
        """
        서버에서 사용 가능한 도구 목록 조회
        
        Returns:
            도구 목록
        """
        if not self.tools:
            # 도구 목록이 없는 경우 서버에 연결하여 도구 목록 가져오기
            connected = await self.connect()
            if not connected:
                logger.error(f"서버 {self.server_url}에 연결할 수 없습니다")
                return []
        return self.tools
    
    async def disconnect(self):
        """서버 연결을 종료합니다."""
        try:
            # 백그라운드 작업 취소
            if hasattr(self, 'keepalive_task') and self.keepalive_task:
                self.keepalive_task.cancel()
                
            if hasattr(self, 'listener_task') and self.listener_task:
                self.listener_task.cancel()
            
            if self.sse_response:
                await self.sse_response.aclose()
                self.sse_response = None
                
            if self.client:
                await self.client.aclose()
                self.client = None
                
            self.initialized = False
            self.connected = False
            self.session_id = None
            self.messages_url = None
            self.tools = []
            logger.info("서버 연결 종료됨")
            
        except Exception as e:
            logger.error(f"연결 종료 중 오류: {str(e)}")
    
    async def invoke_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Optional[Any]:
        """
        도구 호출
        
        Args:
            tool_name: 도구 이름
            arguments: 도구 인자
            
        Returns:
            도구 호출 결과 또는 None
        """
        if not self.initialized or not self.messages_url:
            logger.error("클라이언트가 초기화되지 않았습니다")
            # 자동 연결 시도
            connected = await self.connect()
            if not connected:
                return {"error": "서버 연결 실패"}
            
        # 연결이 끊어진 경우 재연결 시도
        if not self.connected:
            logger.warning("연결이 끊어졌습니다. 재연결 시도 중...")
            connected = await self.connect()
            if not connected:
                return {"error": "서버 재연결 실패"}
            
        logger.info(f"도구 호출: {tool_name} - 인자: {arguments}")
        
        # 도구 존재 여부 확인
        if not any(tool.get("name") == tool_name for tool in self.tools):
            error_msg = f"요청한 도구를 찾을 수 없습니다: {tool_name}"
            logger.error(error_msg)
            return {"error": error_msg}
        
        # 최대 3번 재시도
        max_retries = 3
        retry_delay = 1.0  # 초기 지연 시간 (초)
        
        for attempt in range(1, max_retries + 1):
            try:
                # 연결 상태 확인
                if not self.connected or not self.client:
                    logger.warning("도구 호출 중 연결 상태 확인: 연결 끊김. 재연결 시도 중...")
                    connected = await self.connect()
                    if not connected:
                        return {"error": "도구 호출 중 서버 연결 실패"}
                
                # MCP 표준에 따라 tools/call 메서드 사용
                invoke_msg = self.make_msg(
                    "tools/call",
                    {
                        "name": tool_name,
                        "arguments": arguments  # arguments 키 사용
                    }
                )
                
                msg_id = invoke_msg.get("id")
                
                # 결과 대기 이벤트 생성
                result_event = asyncio.Event()
                self.pending_results[msg_id] = {
                    "event": result_event,
                    "result": None
                }
                
                # 기존 클라이언트 사용 (연결 유지)
                response = await self.post_message(self.client, invoke_msg)
                
                if response and "result" in response:
                    # 즉시 응답이 있는 경우
                    logger.info(f"도구 호출 성공 (즉시 응답): {tool_name}")
                    self.pending_results.pop(msg_id, None)  # 대기 목록에서 제거
                    return response["result"]
                elif response and "error" in response:
                    logger.error(f"도구 호출 오류: {response['error']}")
                    self.pending_results.pop(msg_id, None)  # 대기 목록에서 제거
                    return {"error": response["error"].get("message", "알 수 없는 오류")}
                
                # 응답 대기 (최대 15초)
                logger.info(f"도구 호출 응답 대기 중: {tool_name} (ID: {msg_id})")
                try:
                    await asyncio.wait_for(result_event.wait(), 100.0)
                    result = self.pending_results[msg_id]["result"]
                    self.pending_results.pop(msg_id, None)  # 처리 완료 후 제거
                    
                    if result:
                        logger.info(f"도구 호출 성공 (비동기 응답): {tool_name}")
                        return result
                    else:
                        logger.error(f"도구 호출 결과가 없음: {tool_name}")
                        return {"error": "도구 호출 결과가 없음"}
                        
                except asyncio.TimeoutError:
                    logger.warning(f"도구 호출 응답 대기 시간 초과: {tool_name} (ID: {msg_id})")
                    # 타임아웃 시 다시 시도하거나 대기 상태 반환
                    if attempt < max_retries:
                        logger.info(f"도구 호출 재시도 {attempt}/{max_retries}")
                        continue
                    else:
                        # 마지막 시도에서도 실패한 경우 대기 상태 반환
                        self.pending_results.pop(msg_id, None)  # 대기 목록에서 제거
                        return {"status": "waiting", "message": f"도구 {tool_name} 호출 중"}
                        
            except Exception as e:
                logger.error(f"도구 호출 중 오류 발생 (시도 {attempt}/{max_retries}): {str(e)}")
                if attempt < max_retries:
                    # 지수 백오프 (exponential backoff) 적용
                    wait_time = retry_delay * (2 ** (attempt - 1))
                    logger.info(f"{wait_time}초 후 재시도...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"최대 재시도 횟수 도달. 도구 호출 실패: {tool_name}")
                    return {"error": f"도구 호출 실패: {str(e)}"} 