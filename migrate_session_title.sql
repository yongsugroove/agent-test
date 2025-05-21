-- tb_session_info 테이블에 session_title 컬럼 추가
ALTER TABLE tb_session_info ADD COLUMN session_title VARCHAR(255) NULL COMMENT '세션 제목';

-- 다음과 같이 기존 데이터 업데이트 가능 (선택 사항)
-- 예: 첫 번째 메시지를 기반으로 제목 설정
-- UPDATE tb_session_info si
-- SET si.session_title = (
--     SELECT SUBSTRING(cl.message, 1, 30) 
--     FROM tb_chat_log cl 
--     WHERE cl.session_id = si.session_id AND cl.type = 'user'
--     ORDER BY cl.reg_date ASC 
--     LIMIT 1
-- )
-- WHERE si.session_title IS NULL; 