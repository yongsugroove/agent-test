# MCP SSE 클라이언트

SSE(Server-Sent Events)를 지원하는 MCP(Multi-Client Protocol) 클라이언트 애플리케이션입니다.

## 변경 사항

최신 버전에서 다음과 같은 변경이 이루어졌습니다:

1. Flask에서 FastAPI로 프레임워크 변경
2. 비동기(async/await) 처리 방식 적용
3. SSE(Server-Sent Events) 지원 강화

## 설치 방법

1. 필요한 패키지 설치:

```bash
pip install -r requirements.txt
```

2. `.env` 파일에 OpenAI API 키 설정:

```
OPENAI_API_KEY=your_api_key_here
```

## 실행 방법

### FastAPI 버전 (권장)

다음 명령어로 실행합니다:

```bash
python start.py
```

또는 직접 Uvicorn을 사용할 수도 있습니다:

```bash
uvicorn app_fastapi:app --host 0.0.0.0 --port 8080 --reload
```

### Flask 버전 (레거시)

예전 버전을 사용하려면 다음 명령어로 실행합니다:

```bash
python app.py
```

## API 엔드포인트

### 웹 인터페이스
- `GET /`: 메인 웹 인터페이스

### 채팅
- `POST /api/chat`: 사용자 메시지 처리 및 응답 반환
  - 요청 본문: `{ "message": "사용자 메시지" }`

### 서버 관리
- `GET /api/servers`: 사용 가능한 서버 목록 반환
- `POST /api/servers/add`: 새 서버 추가
  - 요청 본문: `{ "name": "서버 이름", "url": "서버 URL", "enabled": true }`
- `POST /api/servers/toggle`: 서버 활성화/비활성화
  - 요청 본문: `{ "name": "서버 이름", "enabled": true|false }`
- `POST /api/servers/delete`: 서버 삭제
  - 요청 본문: `{ "name": "서버 이름" }`

### 툴 관련
- `GET /api/tools/{server_name}`: 특정 서버의 툴 목록 반환
- `POST /api/sse/{server_name}`: SSE 기반 MCP 서버에 툴 실행 요청
  - 요청 본문: `{ "tool_name": "툴 이름", "inputs": { ... 파라미터 ... } }`

### SSE 스트림
- `GET /api/sse-stream/{server_name}/{tool_name}`: SSE 스트림 제공

## 아키텍처

이 애플리케이션은 다음과 같은 구조로 이루어져 있습니다:

1. FastAPI 웹 서버 (app_fastapi.py)
2. 비동기 MCP 클라이언트 (async_mcp_client.py)
3. 비동기 LLM MCP 클라이언트 (async_llm_mcp_client.py)

## 비동기 동작 방식

1. 클라이언트 초기화는 애플리케이션 시작 시 비동기적으로 수행됩니다.
2. API 엔드포인트는 비동기 함수로 구현되어 효율적인 동시성을 지원합니다.
3. SSE 스트림은 EventSourceResponse를 통해 클라이언트에 전달됩니다.

## SSE 지원

SSE(Server-Sent Events)는 서버에서 클라이언트로의 일방향 실시간 데이터 스트림을 제공합니다. 이 애플리케이션은 다음과 같은 SSE 기능을 지원합니다:

1. MCP 서버의 SSE 엔드포인트 연결
2. 실시간 이벤트 수신 및 처리
3. 클라이언트에 SSE 스트림 제공

## 기능

- MCP 서버 연결 상태 표시
- 사용 가능한 툴 목록 표시
- 툴 선택 및 파라미터 입력
- 툴 실행 및 결과 표시
- 간단한 채팅 인터페이스

## 사용 방법

1. MCP 서버 실행:
   ```bash
   # MCP 서버 디렉토리에서
   python main_sample.py
   ```

2. 클라이언트 실행:
   웹 브라우저에서 `client/index.html` 파일을 열거나, 간단한 웹 서버를 사용하여 호스팅할 수 있습니다.
   
   예를 들어, Python의 내장 HTTP 서버를 사용할 수 있습니다:
   ```bash
   # client 디렉토리에서
   python -m http.server 8080
   ```
   
   그런 다음 웹 브라우저에서 `http://localhost:8080`으로 접속합니다.

3. 클라이언트 사용:
   - 왼쪽 패널에서 사용하려는 툴을 선택합니다.
   - 필요한 파라미터를 입력합니다.
   - "실행" 버튼을 클릭하여 툴을 실행합니다.
   - 실행 결과가 채팅 창에 표시됩니다.

## 서버 URL 설정

기본적으로 클라이언트는 `http://localhost:8000`에서 실행 중인 MCP 서버에 연결을 시도합니다. 다른 URL을 사용하려면 `app.js` 파일에서 `SERVER_URL` 변수를 수정하세요:

```javascript
// 서버 설정
const SERVER_URL = 'http://your-server-url:port';
```

## 지원하는 툴 유형

이 클라이언트는 다음과 같은 유형의 툴 파라미터를 지원합니다:

- 문자열 (string)
- 숫자 (number, integer)
- 불리언 (boolean)
- 배열 (array) - 쉼표로 구분된 값으로 입력

## 문제 해결

- **서버 연결 실패**: MCP 서버가 실행 중인지 확인하고, 서버 URL이 올바른지 확인하세요.
- **CORS 오류**: 서버에서 CORS(Cross-Origin Resource Sharing)를 허용하도록 설정되어 있는지 확인하세요.
- **툴 실행 실패**: 툴 파라미터가 올바른 형식으로 입력되었는지 확인하세요.

## 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다. 