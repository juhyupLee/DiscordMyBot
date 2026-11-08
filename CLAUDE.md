# Discord Bot — Claude Code Adapter

@AGENTS.md

위 `AGENTS.md`가 이 저장소의 정본이다. 이 파일은 AGENTS를 복제하지 않고 Claude Code 전용 차이만 적으며, 충돌하면 AGENTS를 따른다. 봇 동작이 바뀌면 `AGENTS.md`를 고친다.

## Claude Code 진입점

- `/devlog-push` (`.claude/commands/devlog-push.md`) — DEVLOG.md 갱신 후 git commit + push
- 프로젝트 스킬은 `.claude/skills/`, 같은 스킬의 Codex용은 `.agents/skills/`에 있다 (둘 다 gitignore)
