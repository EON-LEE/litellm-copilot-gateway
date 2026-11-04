# litellm-copilot-gateway 작업 규칙

GitHub Copilot 구독 모델을 Claude Code(`ccp`)와 Codex(`ccx`)에서 쓰는 크로스플랫폼 로컬 게이트웨이. 코드는 `src/copilot_gateway/` 파이썬 패키지 하나이며 Windows 네이티브·macOS·Linux·WSL에서 동일하게 동작해야 한다. 아키텍처·검증 현황·알려진 제약은 README.md 참고. 이 파일은 작업 규칙만 담는다.

## 절대 규칙

- **OS 중립 유지** — 새 동작을 bash/PowerShell 스크립트로 추가하지 말고 패키지(파이썬)에 넣는다. 경로는 `pathlib`, 프로세스 조회는 `psutil`, 데이터는 `settings.data_dir()` 아래. 루트의 `*.sh`는 하위 호환 shim일 뿐 로직을 넣지 말 것.
- **생성된 설정(`<data>/config.yaml`, `config-codex.yaml`)을 직접 수정하지 말 것** — `ccgw refresh`가 매번 재생성한다. 라우팅을 바꾸려면 `catalog.py`(`build_config`/`build_codex_config`)를 수정.
- **LiteLLM 패치는 `litellm_patches.py`의 메모리 패치(P1–P7)로만** — site-packages를 수정하지 말 것. 앵커가 안 맞으면 `PatchError`로 기동을 거부해야 하며, 검증을 끄거나 느슨하게 만들지 말 것. LiteLLM 버전(`pyproject.toml` 고정)을 올리면 `ccgw doctor`와 테스트로 앵커를 확인.
- **small/fast 모델(gpt-4o-mini, haiku)의 capi(:4141) 라우팅을 깨지 말 것** — github_copilot 직결로 돌리면 Claude Code WebSearch가 400.
- **명시적인 모델 이름을 다른 모델로 리다이렉트하지 말 것** — 공개는 현재 picker 모델만, 숨김 별칭은 동일 모델의 표기 차이·원본 ID·`[1m]`만. 종료된 모델은 오류.
- **copilot-api는 검증된 2.6.15 고정** (`copilot_api.EXPECTED_VERSION`). 전용 prefix 설치만 쓰고 `npx`/`@latest`/전역 설치를 쓰지 말 것. 버전 변경 시 실제 번들의 identity 패치 앵커를 함께 검증. `useResponsesApiWebSocket=false` 고정(`enforce_http_transport`)을 끄지 말 것 — WebSocket 전송은 gpt-5-mini 스트리밍을 깨뜨린다.
- **WebSearch 보조 모델 동기화(`copilot_api.sync_web_search_model`)를 끄지 말 것** — copilot-api가 설정 파일에 저장한 종료 모델(예: `gpt-5-mini`)이 남으면 Claude Code WebSearch가 모두 실패한다.
- **기본 모델을 코드에 고정하지 말 것** — `ccp` 슬롯은 `catalog.claude_families`, `ccx` 기본값은 `catalog.codex_default`가 실시간 카탈로그에서 고른다. 계정에 없는 계열을 다른 계열 모델로 채우지 말 것.
- **Copilot 인증은 `auth.py` 한 곳에서만** — 카탈로그·LiteLLM·copilot-api가 같은 토큰 폴더의 토큰을 쓴다. copilot-api에는 `copilot_api.token_env`(환경변수)로만 넘기고, 서비스별 로그인 지문 확인(`gateway.run_service`)을 끄지 말 것. 계정 이름을 코드에 넣거나, `GH_TOKEN`·gh 키링 같은 Git 자격 증명을 Copilot 인증에 쓰지 말 것. 토큰 값을 출력·로그·명령 인자에 넣지 말 것.
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

유료 스모크는 명시적으로 요청된 경우에만. `ccgw env claude`로 키를 얻어 `http://127.0.0.1:4000/v1/messages`에 보낸다. 모델은 `ccgw models`의 `Defaults` 줄에 나온 현재 기본값을 쓴다.

1. **WebSearch**: `"model"`에 haiku 기본값, `"tools":[{"type":"web_search_20250305","name":"web_search"}]`, `"tool_choice":{"type":"tool","name":"web_search"}` → `server_tool_use` + `web_search_tool_result`.
2. **스트리밍**: `"stream":true`로 sonnet 기본값과 공개 GPT 모델 → `message_stop`으로 끝나고 "list index out of range" 없음.
3. **메인 모델**: `ccp` 메인 기본값으로 단순 질문 200.
4. **Codex**: `ccx exec --skip-git-repo-check -s workspace-write -m <model> "..."`로 GPT·Claude·Gemini·Grok 각 1회 툴 루프 (Claude는 P5/P6, Grok은 `hooks.tool_filter` 회귀 확인용).

:4000/:4001에서만 나면 LiteLLM, :4141 직접 호출에서도 나면 copilot-api/업스트림 문제.

## 트러블슈팅 메모

- `Anthropic CountTokens API error: 401` 경고는 무해.
- Codex가 "OutputTextDelta without active item"을 내면 P6, Claude가 "assistant message prefill" 400이면 P5 회귀.
- Windows 콘솔(cp949)에서 JSON/로그를 다룰 때는 UTF-8을 명시.
