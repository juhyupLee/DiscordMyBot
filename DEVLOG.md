# DEVLOG

## 2026-04-09
- Claude CLI 출력을 `stream-json`으로 변경하여 Discord에 중간 작업 상태 실시간 표시 (도구 사용, 텍스트 미리보기)
- Unity AAB 빌드 지원을 위해 `ANDROID_HOME`, `JAVA_HOME` 환경변수 자동 전달
- `start_hidden.vbs`를 관리자 권한(runas)으로 실행하도록 변경
- 로그 파일을 `logs/` 폴더로 정리
- `.gitignore` 정리 (logs, venv, png 등 추가)
- 불필요한 파일 정리 (테스트 이미지, venv, __pycache__ 삭제)
- `/devlog-push` 커맨드 추가
- **대화형 모드로 전환**: `-p -` 플래그 제거, 프로세스를 재활용하여 진짜 대화형 Claude 지원
- 대기 메시지에 현재 작업 상태 표시 (`⏳ 대기 N번째 | 현재: ...`)
- 불필요한 배치 파일 정리 (register_service/task, unregister_service/task, start.bat 삭제)
- CLAUDE.md 현재 상태 반영하여 갱신
