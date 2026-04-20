# Troubleshoot

## Claude CLI 대화형 모드 vs -p 모드 문제

**증상**: 봇 응답이 10초 이상 걸리거나 아예 안 나옴. 스트리밍 표시 안 됨. 일부 기능 동작 안 함.

**원인**: 대화형 모드(no `-p`)에서 `--output-format stream-json` 사용 시:
- `--include-partial-messages` 플래그가 **`-p` 모드에서만 동작**하므로 무시됨
- `stream_event` (토큰 단위 `content_block_delta`)가 발생하지 않음
- `assistant` 이벤트가 전체 텍스트 완성 후 한 번에 와서 실시간 스트리밍 불가

**해결**: `-p -` 모드로 전환
- 매 메시지마다 새 프로세스 생성, stdin으로 프롬프트 전달 후 닫기
- `--resume` 플래그로 세션 ID 유지하여 대화 맥락 보존
- `--include-partial-messages`로 토큰 단위 스트리밍 수신

**관련 플래그 정리**:
```
-p -                          stdin에서 프롬프트 읽기 (print 모드)
--output-format stream-json   실시간 JSON 스트리밍 출력
--include-partial-messages    토큰 단위 partial message 포함 (-p 모드 전용!)
--resume <session_id>         기존 세션 이어서 대화
```

**참고**: 다른 채널이 느릴 경우, 오래된 세션 컨텍스트가 원인일 수 있음. `리셋` 명령으로 세션 초기화하면 해결.
