# DEVLOG

## 2026-04-09
- Claude CLI 출력을 `stream-json`으로 변경하여 Discord에 중간 작업 상태 실시간 표시 (도구 사용, 텍스트 미리보기)
- Unity AAB 빌드 지원을 위해 `ANDROID_HOME`, `JAVA_HOME` 환경변수 자동 전달
- `start_hidden.vbs`를 관리자 권한(runas)으로 실행하도록 변경
- 로그 파일을 `logs/` 폴더로 정리
- `.gitignore` 정리 (logs, venv, png 등 추가)
- 불필요한 파일 정리 (테스트 이미지, venv, __pycache__ 삭제)
- `/devlog-push` 커맨드 추가
