# litellm-copilot-gateway 작업 규칙

GitHub Copilot 구독 모델을 Claude Code(`ccp`)와 Codex(`ccx`)에서 쓰는 크로스플랫폼 로컬 게이트웨이. 코드는 `src/copilot_gateway/` 파이썬 패키지 하나이며 Windows 네이티브·macOS·Linux·WSL에서 동일하게 동작해야 한다. 아키텍처·검증 현황·알려진 제약은 README.md 참고. 이 파일은 작업 규칙만 담는다.

## 절대 규칙

- **OS 중립 유지** — 새 동작을 bash/PowerShell 스크립트로 추가하지 말고 패키지(파이썬)에 넣는다. 경로는 `pathlib`, 프로세스 조회는 `psutil`, 데이터는 `settings.data_dir()` 아래. 루트의 `*.sh`는 하위 호환 shim일 뿐 로직을 넣지 말 것.
- **생성된 설정(`<data>/config.yaml`, `config-codex.yaml`)을 직접 수정하지 말 것** — `ccgw refresh`가 매번 재생성한다. 라우팅을 바꾸려면 `catalog.py`(`build_config`/`build_codex_config`)를 수정.
- **LiteLLM 패치는 `litellm_patches.py`의 메모리 패치(P1–P6)로만** — site-packages를 수정하지 말 것. 앵커가 안 맞으면 `PatchError`로 기동을 거부해야 하며, 검증을 끄거나 느슨하게 만들지 말 것. LiteLLM 버전(`pyproject.toml` 고정)을 올리면 `ccgw doctor`와 테스트로 앵커를 확인.
- **small/fast 모델(gpt-4o-mini, haiku)의 capi(:4141) 라우팅을 깨지 말 것** — github_copilot 직결로 돌리면 Claude Code WebSearch가 400.
- **명시적인 모델 이름을 다른 모델로 리다이렉트하지 말 것** — 공개는 현재 picker 모델만, 숨김 별칭은 동일 모델의 표기 차이·원본 ID·`[1m]`만. 종료된 모델은 오류.
- **copilot-api는 검증된 2.6.15 고정** (`copilot_api.EXPECTED_VERSION`). 전용 prefix 설치만 쓰고 `npx`/`@latest`/전역 설치를 쓰지 말 것. 버전 변경 시 실제 번들의 identity 패치 앵커를 함께 검증.
- **프로세스는 검증된 PID만 종료** — `services.stop`은 포트 소유 PID의 명령줄을 확인한다. 이름 기반 kill(`pkill`, `taskkill /IM`)이나 강제 종료로 우회하지 말 것.
- `.env`(마스터키)와 크레덴셜은 절대 커밋 금지.

## 자주 쓰는 작업

```bash
ccgw status                 # 포트 · 소유자 · readiness
ccgw restart [claude|codex] # LiteLLM 재시작 (설정 리로드)
ccgw refresh                # 모델 목록 갱신
ccgw logs claude -f         # 로그 (capi | claude | codex)
ccgw doctor                 # 전제조건 + 패치 앵커
```

운영 중인 게이트웨이와 충돌하지 않게 개발할 때는 `CCGW_HOME`과 `CCGW_PORT`/`CCGW_CODEX_PORT`/`CCGW_CAPI_PORT`를 별도로 지정.

## 변경 후 검증

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
# Windows: $env:LITELLM_LOCAL_MODEL_COST_MAP="True"; .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

테스트는 오프라인이고 OS 중립이어야 한다(bash/lsof/npx 의존 금지). LiteLLM import 훅이 필요한 테스트는 `helpers.run_python`으로 새 인터프리터에서 실행.

유료 스모크는 명시적으로 요청된 경우에만. `ccgw env claude`로 키를 얻어 `http://127.0.0.1:4000/v1/messages`에:

1. **WebSearch**: `"model":"claude-haiku-4-5"`, `"tools":[{"type":"web_search_20250305","name":"web_search"}]`, `"tool_choice":{"type":"tool","name":"web_search"}` → `server_tool_use` + `web_search_tool_result`.
2. **스트리밍**: `"stream":true`로 claude-sonnet-5와 공개 GPT 모델 → `message_stop`으로 끝나고 "list index out of range" 없음.
3. **메인 모델**: claude-opus-5 단순 질문 200.
4. **Codex**: `ccx exec --skip-git-repo-check -m <model> "..."`로 GPT·Claude·Gemini 각 1회 툴 루프 (Claude는 P5/P6 회귀 확인용).

:4000/:4001에서만 나면 LiteLLM, :4141 직접 호출에서도 나면 copilot-api/업스트림 문제.

## 트러블슈팅 메모

- `Anthropic CountTokens API error: 401` 경고는 무해.
- Codex가 "OutputTextDelta without active item"을 내면 P6, Claude가 "assistant message prefill" 400이면 P5 회귀.
- Windows 콘솔(cp949)에서 JSON/로그를 다룰 때는 UTF-8을 명시.
