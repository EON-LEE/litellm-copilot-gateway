# litellm-copilot-gateway

GitHub Copilot 구독 모델(Claude · GPT · Gemini · Grok …)을 **Claude Code**와 **OpenAI Codex**(CLI · VS Code 확장)에서 쓰는 로컬 게이트웨이.
Windows 네이티브 · macOS · Linux · WSL 에서 **같은 명령**으로 동작한다 (WSL 불필요).

| 명령 | 역할 |
|---|---|
| `ccp` | Copilot 백엔드로 Claude Code 실행 (평소 `claude`는 영향 없음) |
| `ccx` | Copilot 백엔드로 Codex CLI 실행, `ccx code` = VS Code(별도 프로필) + Codex 확장 |
| `ccgw` | 게이트웨이 관리: `setup` `login` `start` `stop` `restart` `status` `refresh` `models` `logs` `env` `doctor` `paths` |

## 설치

사전 준비: [uv](https://docs.astral.sh/uv/), Node.js 20+(`node`, `npm`), Copilot 구독 GitHub 계정, 쓰려는 클라이언트(`claude` 및/또는 `codex` 또는 VS Code `openai.chatgpt` 확장).

```bash
uv tool install --python 3.13 git+https://github.com/EON-LEE/litellm-copilot-gateway
ccgw setup      # 마스터키 생성 → copilot-api 2.6.15 설치 → GitHub device 로그인 → 모델 설정 생성
ccgw doctor     # 전제조건 · LiteLLM 패치(P1–P6) · copilot-api 핀 확인
```

PowerShell / bash / zsh 모두 동일. 업데이트는 `uv tool upgrade litellm-copilot-gateway`.
LiteLLM은 패키지 의존성으로 **1.95.0 + fastapi 0.140.6**에 고정되어 함께 설치된다 (별도 `uv tool install litellm` 불필요).

사내망 등에서 npm 미러에 `@jeffreycao/copilot-api@2.6.15`가 없으면:

```powershell
$env:CCGW_NPM_REGISTRY = "https://registry.npmjs.org/"       # 레지스트리 지정
# 또는 다른 머신에서 `npm pack @jeffreycao/copilot-api@2.6.15` 한 tarball 사용
$env:CCGW_COPILOT_API_SOURCE = "C:\path\jeffreycao-copilot-api-2.6.15.tgz"
ccgw setup
```

## 사용법

```bash
ccp                              # Claude Code (기본 claude-sonnet-5)
ccp --model claude-gpt-5.5       # 다른 Copilot 모델
ccx                              # Codex TUI (기본 gpt-5.5 계열)
ccx -m claude-opus-5             # Codex에서 Claude
ccx exec "fix the failing test"  # codex 인자 그대로 전달
ccx code .                       # VS Code(별도 프로필)에서 Codex 확장을 게이트웨이로
ccgw models                      # 실제 모델명 · ID · 컨텍스트 · 입력/출력 한도
ccgw status                      # 포트 · 소유 프로세스 · readiness
```

- `ccp`/`ccx`는 실행마다 Copilot 카탈로그를 새로 받아 설정을 갱신하고(`--no-refresh`로 생략), 필요한 서비스만 띄운다. 갱신 실패 시 기존 설정으로 기동.
- `ccp`는 항상 `--dangerously-skip-permissions`로 실행되고, 사용자가 준 `--model`·환경변수는 보존. 기본값: opus=`claude-opus-5`, sonnet=`claude-sonnet-5`, haiku=`claude-haiku-4.5`, small-fast=`gpt-5-mini`.
- `ccx`는 격리된 `CODEX_HOME`(`<data>/codex`)을 쓰므로 평소 `~/.codex`(ChatGPT 로그인 등)는 그대로. 기본 모델 변경: `CCX_MODEL`. 실행 파일 지정: `CCX_CODEX`(없으면 PATH의 `codex`, 그다음 VS Code 확장 번들 `codex`).
- `ccx code` 첫 실행(별도 프로필): VS Code 로그인 안내는 "Continue without Signing In"으로 건너뛰고, 폴더를 **Trust**해야 Codex 확장이 켜진다(Restricted Mode에선 비활성). ChatGPT 로그인 없이 바로 게이트웨이로 대화된다. Windows에서 뜨는 "Finish Windows setup" 카드는 Codex 샌드박스 설정이며, 파일 수정·명령 실행을 하려면 진행해야 한다(대화만 할 땐 불필요).
- 다른 셸/IDE에 직접 연결하려면 `ccgw env claude|codex --shell powershell|posix|cmd`.

### 기존 bash 버전에서 이전

예전 `~/litellm-copilot-gateway/*.sh`는 새 명령으로 넘겨주는 얇은 shim으로 남아 있어 기존 알리아스(`ccp=…/claude-copilot.sh`)도 동작한다. 새 설치 후에는 알리아스를 지우고 `ccp`/`ccx`/`ccgw`를 직접 쓰면 된다.

- 마스터키: 레거시 `~/litellm-copilot-gateway/.env`의 키를 첫 실행 때 그대로 가져온다.
- 로그인 토큰: 같은 위치(`~/.config/litellm/github_copilot`)를 재사용 → 재로그인 불필요.
- 포트 4000에 bash 시절 LiteLLM이 떠 있으면 `ccgw restart claude`가 확인 후 교체한다.

## 아키텍처

```
Claude Code ─/v1/messages──▶ LiteLLM :4000 (Anthropic API) ─┐
Codex CLI/VS Code ─/v1/responses─▶ LiteLLM :4001 (Responses) ─┼─▶ copilot-api :4141 ─▶ api.githubcopilot.com
                                                             └─▶ github_copilot provider (chat-only 모델)
```

- 모든 서비스는 `127.0.0.1` 전용, LiteLLM은 마스터키 인증. 포트: `CCGW_PORT`/`CCGW_CODEX_PORT`/`CCGW_CAPI_PORT`.
- **LiteLLM 패치는 메모리에서 적용**: 게이트웨이가 띄우는 LiteLLM 프로세스에서만 import 시점에 소스를 앵커 치환한다. site-packages를 수정하지 않으므로 업그레이드로 지워지지 않고, 앵커가 바뀌면 기동 자체를 거부한다.
- **copilot-api는 전용 prefix에 2.6.15 고정 설치**(`<data>/copilot-api/2.6.15`). 전역 npm/`npx @latest`를 쓰지 않는다. 기동 전 번들의 암묵적 small-model 교체를 원자적으로 패치하고, `modelMappings`/`claudeAutoModel` 설정이 있으면 기동 거부.
- 프로세스 정지는 포트 소유 PID의 명령줄을 검증한 뒤 그 PID(와 동일 명령의 venv 런처 부모)에만 신호. 다른 프로세스는 거부하고 강제 종료하지 않는다.

| 패치 | 대상 | 내용 |
|---|---|---|
| P1 | github_copilot authenticator | device 로그인 대기 1분 → 10분 |
| P2 | Anthropic→OpenAI 어댑터 | 툴 변환 후 툴이 없으면 강제 `tool_choice` 제거 (WebSearch 400) |
| P3 | Anthropic SSE 어댑터 | Copilot의 usage-only 빈 `choices` 청크 처리 (스트리밍 크래시) |
| P4 | `/v1/models` | 허용 목록의 Copilot 표시명·설명·컨텍스트 노출 (Claude Code 모델 피커) |
| P5 | Responses→Chat 변환 | Codex 히스토리의 빈 assistant 메시지 제거 · 텍스트를 tool call 앞으로 (Claude prefill 400) |
| P6 | Responses 스트림 | reasoning/tool call로 시작한 스트림에서도 message item 생성 (Codex 텍스트 유실) |

### 데이터 위치 (`ccgw paths`)

| OS | 기본 경로 (`CCGW_HOME`으로 변경) |
|---|---|
| Windows | `%LOCALAPPDATA%\copilot-gateway` |
| macOS | `~/Library/Application Support/copilot-gateway` |
| Linux/WSL | `${XDG_DATA_HOME:-~/.local/share}/copilot-gateway` |

`.env`(마스터키), `config.yaml`(Claude용), `config-codex.yaml`(Codex용, 자동 생성 — 직접 편집 금지), `logs/`, `codex/`(CODEX_HOME), `copilot-api/`.
GitHub 토큰: `~/.config/litellm/github_copilot/` (`GITHUB_COPILOT_TOKEN_DIR`로 변경).

## 모델 1:1 매핑

**Copilot 실제 모델 1개 = 선택 항목 1개 = 실제 호출 대상 1개.**

- `model_picker_enabled=true`인 chat 모델만 공개. `Internal only` 및 `policy.state`가 비활성인 모델 제외. 고정 allowlist 없음 → 새 모델은 자동 반영.
- **Claude Code (:4000)**: Claude Code는 ID에 `claude`가 있는 모델만 받으므로 비-Claude는 `claude-<Copilot ID>`로 공개(화면 이름은 실제 GPT/Gemini 이름), 원본 ID는 숨김 별칭. 전체 컨텍스트 ≥ 1M 모델은 `[1m]` 숨김 별칭.
- **Codex (:4001)**: Copilot 원본 ID 그대로 (`gpt-5.5`, `claude-opus-5`, `gemini-3.8-flash` …). GPT(Responses) 모델은 copilot-api의 `/v1/responses`, Claude는 Messages, chat-only는 github_copilot provider. xAI(Grok)는 Responses 경로 + 미지원 툴 타입 제거 훅(아래 제약 참고).
- 같은 버전의 표기 차이만 허용(`claude-haiku-4-5` → 실제 `claude-haiku-4.5`). 다른 모델·구버전→신버전 대체 없음. 미등록·종료된 모델은 오류.
- 한도는 카탈로그 선언값(컨텍스트·입력·출력·비스트리밍 출력)을 그대로 쓰고 추정하지 않음. 누락·형식 오류·충돌 시 갱신을 중단하고 기존 설정 유지(fail-closed). 내용이 같으면 파일을 다시 쓰지 않음.

## WebSearch 동작 방식

Claude Code의 WebSearch는 Anthropic 서버사이드 툴(`web_search_20250305`)이지만, copilot-api의 `messageApiWebSearchModel` 기능이 web_search 단독 `/v1/messages` 요청을 Responses 지원 GPT 모델의 hosted `web_search`로 재라우팅하고 결과를 `server_tool_use` + `web_search_tool_result`로 재구성한다(외부 검색 API 키 불필요).

- 명시적 검색 보조 동작이며 일반 모델 대체가 아님. Haiku 일반 요청은 실제 Haiku로 간다.
- 그래서 **small/fast · haiku 모델은 반드시 copilot-api(:4141) 경유**. LiteLLM `github_copilot` 직결로 바꾸면 WebSearch가 400.

## 알려진 제약

- **web_fetch 서버 툴**: 게이트웨이 전체에서 불가. Claude Code의 WebFetch는 클라이언트 실행이라 실사용 영향 없음.
- **web_search + 다른 툴 혼합 요청**: copilot-api가 web_search를 조용히 제거. Claude Code는 단독으로 보내므로 무관.
- **count_tokens**: 로컬 근사치(tools 미집계). `Anthropic CountTokens API error: 401` 경고는 무해.
- **github_copilot 직결 경로 usage**: input_tokens 과소보고 → 비용 집계 신뢰 불가.
- **grok**: tools 없는 bare 요청은 copilot-api 버그로 400. 에이전트는 항상 tools를 보내므로 무관.
- **grok + Codex**: Copilot의 xAI `/responses`가 Codex의 `namespace`(멀티 에이전트) 툴과 hosted `web_search` 툴을 400/422로 거부. Codex 설정은 xAI 모델에 한해 이 두 툴만 요청에서 제거(`copilot_gateway.hooks`, 카탈로그 `unsupported_tool_types`). 셸·파일 편집 등 나머지 툴은 정상, Grok에서는 Codex 웹검색·서브에이전트만 불가.
- **인증 오류 코드**: DB 없는 LiteLLM 1.95.0은 키 누락 500, 잘못된 키 400. 요청은 거부됨.
- **Codex 기능 범위**: Codex의 ChatGPT 전용 기능(클라우드 태스크, 이미지 생성 등)은 사용 불가. 로컬 에이전트·툴콜·추론은 동작.
- **ToS**: 에디터 외부의 Copilot API 사용은 비공식 영역. 개인·로컬·수동 규모로만 사용 권장.

## 검증 현황

| 항목 | 상태 |
|---|---|
| Windows 네이티브 기동/정지/재시작 (3 서비스) | ✅ |
| Codex CLI: GPT-5.5 · GPT-5.4-mini · Claude Opus/Sonnet 5 · Haiku 4.5 · Gemini 3.8 Flash 멀티 툴 루프 | ✅ (P5/P6 적용 후) |
| Claude Code: 스트리밍 · 툴콜 · WebSearch · thinking · 비전 · 캐싱 (bash 버전, 2026-08) | ✅ 동일 라우팅 유지 |
| web_fetch 서버 툴 | ❌ (위 제약) |

## 개발 / 테스트

```bash
uv venv --python 3.13 .venv && uv pip install --python .venv -e .
LITELLM_LOCAL_MODEL_COST_MAP=True .venv/bin/python -m unittest discover -s tests -v
# Windows: $env:LITELLM_LOCAL_MODEL_COST_MAP="True"; .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- 테스트는 오프라인이며 유료 추론·실제 토큰·실제 서비스를 쓰지 않는다. CI는 ubuntu/windows/macOS 매트릭스.
- 운영 중인 게이트웨이와 충돌 없이 개발하려면 별도 데이터 디렉터리·포트를 쓴다:
  `CCGW_HOME=/tmp/ccgw-dev CCGW_PORT=14000 CCGW_CODEX_PORT=14001 CCGW_CAPI_PORT=14141 ccgw start --all`
- 설치된 copilot-api 번들까지 검사하려면 `CCGW_TEST_HOME=<data dir>`.
- 유료 스모크(WebSearch, 스트리밍, 메인 모델)는 필요할 때만: `ccgw env claude`로 키를 얻어 `/v1/messages`에 직접 요청.

## 보안

- 모든 서비스 `127.0.0.1` 바인딩, LiteLLM 마스터키는 랜덤 생성되어 `<data>/.env`(600)에만 저장.
- GitHub 토큰은 프로세스 인자로 넘기지 않고 파일 스토어(600)로만 전달.
- 클라이언트 환경변수는 `ccp`/`ccx`의 자식 프로세스에만 적용.
