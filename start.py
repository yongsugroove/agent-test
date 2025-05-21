import uvicorn

if __name__ == "__main__":
    print("MCP SSE 클라이언트 시작 중...")
    uvicorn.run("app_fastapi:app", host="0.0.0.0", port=58010, reload=False) 