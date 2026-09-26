# GitHub Copilot → Claude Code 게이트웨이

GitHub Copilot 구독의 모델들(Claude, GPT-5.x, Gemini, Grok)을 Claude Code에서 쓰기 위한 로컬 전용(127.0.0.1) 게이트웨이. LiteLLM이 Claude Code용 Anthropic 호환 엔드포인트를 노출하고, 내부적으로 GitHub Copilot API로 요청을 라우팅한다.

## 설치 (처음 셋업 / 다른 환경으로 이전 시)

### 0. 사전 준비
- **GitHub Copilot 구독**이 있는 GitHub 계정 (Individual/Business 무관, 이 저장소는 인증 방법을 다루지 않음 — 아래 2단계에서 이 리포와 무관하게 최초 1회 device-flow 로그인만 필요)
- macOS 또는 Linux/WSL + `bash`, `python3` (3.9 이상)
- `uv` (Python 툴 관리자) — `brew install uv`
- `node`/`npx` (copilot-api 실행용) — `brew install node`
- `jq`, `curl`, `lsof` (보통 macOS/homebrew에 기본 포함)
- `gh` CLI (이 저장소를 클론/관리할 때만 필요, 게이트웨이 동작 자체와는 무관)

### 1. 이 저장소 클론
```bash
git clone <이 저장소 URL> ~/litellm-copilot-gateway
cd ~/litellm-copilot-gateway
```

### 2. litellm 설치 (fastapi 버전 고정 필수 — "litellm 업그레이드 시" 섹션 참고)
```bash
uv tool install --python 3.13 'litellm[proxy]==1.95.0' --with 'fastapi==0.140.6'
./apply-patches.sh     # LiteLLM 패치 4종 적용 (인증·tool_choice·SSE·모델 설명)
```

### 3. 마스터 키 생성 (`.env`, git에 커밋되지 않음)
```bash
echo "LITELLM_MASTER_KEY=$(openssl rand -hex 32)" > .env
chmod 600 .env
```

### 4. GitHub Copilot 최초 로그인 (최초 1회, device flow)
```bash
litellm --config config.yaml --host 127.0.0.1 --port 4000
```
콘솔에 뜨는 `https://github.com/login/device` 링크와 코드로 브라우저에서 로그인 완료 후 `Ctrl+C`로 종료. `~/.config/litellm/github_copilot/`에 토큰이 저장된다 (이후로는 자동 갱신, 재로그인 불필요 — 아래 "재로그인 필요 여부" 참고).
> 참고: 최초 실행 시 `config.yaml`이 아직 없으므로, 먼저 `./refresh-models.sh`를 한 번 실패시켜서라도 실행해 이 로그인 절차를 트리거하거나, `refresh-models.sh`가 내부적으로 쓰는 것과 동일한 토큰 발급 절차를 거치면 된다. 가장 간단한 방법은 5단계의 `refresh-models.sh`를 먼저 실행하는 것 — 최초 실행 시 아직 `~/.config/litellm/github_copilot/access-token`이 없으면 안내 메시지가 뜬다.

### 5. 모델 목록 생성 + 스택 기동
```bash
./refresh-models.sh      # Copilot 최신 모델 목록으로 config.yaml 생성
./start-proxy.sh         # copilot-api(:4141) + litellm(:4000) 기동
```

### 6. Claude Code 실행
```bash
./claude-copilot.sh
```

## 아키텍처

```
Claude Code ──/v1/messages──▶ LiteLLM :4000 (127.0.0.1, 마스터키 인증)
                              ├─ Claude / Responses 모델 ─▶ copilot-api :4141 ─▶ GitHub Copilot
                              └─ chat-only 모델 ──────────▶ github_copilot provider ─▶ GitHub Copilot
```

- **litellm 1.95.0** + **fastapi==0.140.6** (0.140.7+ 부팅 호환 문제: litellm#35763).
- **copilot-api 2.6.15** = `@jeffreycao/copilot-api` (caozhiyuan/copilot-api). Anthropic 요청을 Copilot 네이티브 Messages 또는 Responses로 전달. 시작 스크립트와 래퍼 모두 검증된 버전을 고정하며 `@latest`를 실행하지 않음.
- `anthropic/<id>`는 프로토콜 어댑터 지정. Anthropic 직접 과금 경로가 아니라 `localhost:4141` 경유.

## 모델 1:1 매핑

**Copilot 실제 모델 1개 = 선택 항목 1개 = 실제 호출 대상 1개.**

- `model_picker_enabled=true`인 chat 모델만 공개. `Internal only` 및 명시적으로 비활성인 `policy.state` 제외. 정책 필드가 없는 모델은 표시 플래그 기준.
- 2026-09-27 조회: 전체 chat 41개 중 picker 대상 26개, 내부용 1개 제외 후 **25개 공개**. 표시 여부는 실호출 권한 보장이 아님.
- 표시 이름은 Copilot `name`, 설명은 `Copilot · 컨텍스트 … · 입력 … · 출력 …`. 공급자 표시는 `github_copilot`.
- Claude Code 2.1.263 검색은 ID에 `claude`/`anthropic`이 들어간 항목만 수용. 비-Claude의 공개 호출 ID는 `claude-<Copilot ID>`지만 화면 이름은 실제 GPT/Gemini/Grok 이름. 원본 ID는 동일 모델을 가리키는 숨김 별칭.
- 같은 버전의 표기 차이만 허용: `claude-haiku-4-5` → **실제 `claude-haiku-4.5`**. `Fable → Opus`, `Haiku → GPT`, 구버전 → 신버전 대체는 없음.
- 와일드카드 제거. 미등록·종료된 모델은 오류로 거부하며 다른 모델로 대체하지 않음.
- `[1m]`은 별도 선택 항목이 아님. 전체 컨텍스트가 1,000,000 이상인 모델에만 같은 모델의 숨김 호환 표기로 유지.

같은 2026-09-27 카탈로그로 이전 생성기(`cfb0cdc`)를 재현하면 와일드카드 포함 135개 항목이 생기고, 실제 대상 40개 모두 원본·`claude-`·`[1m]` 등의 중복 항목을 가짐. 새 생성기는 공개 25개와 숨김 별칭 55개(원본 비-Claude ID 19개, Claude 표기 차이 4개, `[1m]` 32개)를 분리. 원본 카탈로그의 GPT-4o/4o-mini/4.1 및 GPT-3.5/4 스냅샷 중복은 picker 비대상이므로 노출하지 않음. 이 숫자는 조회 시점 기준이며 코드의 고정 목록이 아님.

| 실제 Copilot 모델 | 전체 컨텍스트 | 최대 입력 | 최대 출력 |
|---|---:|---:|---:|
| GPT-6 Astra | 1,050,000 | 1,050,000 | 128,000 |
| Claude Opus 5.5 | 1,000,000 | 1,000,000 | 128,000 |
| Claude Opus 5 / Sonnet 5 | 1,000,000 | 936,000 | 64,000 |
| Claude Haiku 4.5 | 200,000 | 136,000 | 64,000 |
| GPT-5 mini | 264,000 | 128,000 | 64,000 |

단위: 토큰. 2026-09-27 Copilot 카탈로그 선언값이며 게이트웨이 최대 부하 실측값이 아님. 입력 한도를 `컨텍스트 − 출력`으로 추정하지 않음. 위 Claude 모델의 비스트리밍 출력 상한은 별도 **16,000**. 한도 누락·형식 오류 시 다른 공급자의 값으로 추정하지 않고 갱신을 중단해 기존 설정 유지.

`model_catalog.py`가 목록·라우팅·설명을 함께 생성. `config.yaml`은 JSON 문법으로 직렬화한 유효한 YAML이며 직접 편집 금지.

## 사용법

```bash
~/litellm-copilot-gateway/start-proxy.sh        # 스택 기동 (이미 떠있으면 스킵)
~/litellm-copilot-gateway/claude-copilot.sh     # Copilot 백엔드로 Claude Code 실행
~/litellm-copilot-gateway/claude-copilot.sh --model gpt-5.5   # 모델 지정
```

### 셸 알리아스 (선택)

`~/.zshrc`(macOS) 또는 `~/.bash_aliases`(Linux/WSL)에 추가:

```bash
# litellm-copilot-gateway
alias claude-copilot="$HOME/litellm-copilot-gateway/claude-copilot.sh"
alias ccp="$HOME/litellm-copilot-gateway/claude-copilot.sh"
alias copilot-start="$HOME/litellm-copilot-gateway/start-proxy.sh"
alias copilot-stop="$HOME/litellm-copilot-gateway/stop-proxy.sh"
alias copilot-restart="$HOME/litellm-copilot-gateway/restart-proxy.sh"
alias copilot-models="$HOME/litellm-copilot-gateway/list-models.sh"
alias copilot-refresh="$HOME/litellm-copilot-gateway/refresh-models.sh && $HOME/litellm-copilot-gateway/restart-proxy.sh"
```

적용: `source ~/.bash_aliases` (또는 새 셸) 후 `ccp`로 바로 실행.

- 세션 안: `/model`로 실제 모델명과 컨텍스트 설명을 확인하고 전환. 목록 갱신 후 새 `ccp` 세션에서 discovery 캐시 갱신.
- 평소 `claude` 명령(진짜 Anthropic)은 영향 없음 — 환경변수는 이 스크립트의 자식 프로세스에만 적용.
- 기본값: main=`claude-sonnet-5`, opus=`claude-opus-5`, **haiku=`claude-haiku-4.5`**. 이전 클라이언트의 `ANTHROPIC_SMALL_FAST_MODEL`만 명시적으로 `gpt-5-mini` 유지. 환경변수와 `--model`/`--model=...`로 선택한 값 보존.
- `claude-copilot.sh`는 항상 `--dangerously-skip-permissions`로 실행됨 (권한 프롬프트 없이 자동 승인 — Copilot 백엔드 전용 로컬 세션이므로 실제 Anthropic `claude` 명령에는 영향 없음)

## 스크립트

| 파일 | 역할 |
|---|---|
| `start-proxy.sh` | copilot-api(:4141) + litellm(:4000) 기동, 둘 다 127.0.0.1 전용. 두 `/v1/models` readiness가 모두 성공해야 완료 |
| `stop-proxy.sh` / `restart-proxy.sh` | 포트 소유 PID와 정확한 실행 파일·설정 확인 후 정지 / litellm만 재시작(설정 리로드). 타 프로세스나 종료 시간 초과 시 강제 종료하지 않고 실패 |
| `claude-copilot.sh` | Claude Code 런처 (게이트웨이 자동 기동 + 실행마다 모델 목록 자동 refresh 포함) |
| `refresh-models.sh` | 실시간 카탈로그 검증 후 config.yaml 원자적 교체. 내용 동일 시 파일·백업 유지 |
| `model_catalog.py` | 사용자용 모델 필터, 1:1 라우팅, 숨김 동일모델 별칭, 실제 이름·한도 생성 |
| `list-models.sh` | 같은 필터로 실제 모델명·ID·전체 컨텍스트·입력·출력·비스트리밍 상한 조회 |
| `apply-patches.sh` | LiteLLM 패치: 1 인증 대기, 2 tool_choice, 3 SSE, 4 모델 이름·설명·한도. 새 프록시 시작 전 자동 실행 |
| `start-copilot-api.py` | PATH의 패키지·버전·import를 검증하고 암묵적 small-model 교체 차단 후 실행. 타모델 매핑 설정은 시작 거부 |

## 새 모델이 나오면

`claude-copilot.sh`(`ccp`)는 실행할 때마다 자동으로 아래를 먼저 돌리므로, 평소엔 아무것도 안 해도 새 모델이 반영된다:

```bash
~/litellm-copilot-gateway/refresh-models.sh && ~/litellm-copilot-gateway/restart-proxy.sh
```
새 모델도 사용자용 표시·정책·한도 검증을 통과하면 자동 반영. 고정 최신 모델 allowlist 없음. **패키지 버전 고정과 모델 목록 갱신은 별개**이므로 새 모델도 계속 발견됨. 갱신 실패 시 기존 설정으로 두 서비스를 확인·기동하며, 기존 설정조차 없으면 중단. LiteLLM만 살아 있고 copilot-api가 죽은 상태를 정상으로 오인하지 않음. 성공 시 프록시가 재시작되므로 다른 진행 중 세션에 잠깐 영향이 있을 수 있음.

## litellm 업그레이드 시 (주의)

```bash
uv tool install --python 3.13 'litellm[proxy]==<ver>' --with 'fastapi==0.140.6'
~/litellm-copilot-gateway/apply-patches.sh     # 패치 재적용 필수
~/litellm-copilot-gateway/restart-proxy.sh
```
fastapi 핀은 litellm#35763 (PR #35389/#35139/#35773/#35858 중 하나) 머지 후 제거 가능.

## copilot-api 업그레이드 시

`start-proxy.sh`의 패키지 버전과 `start-copilot-api.py`의 `EXPECTED_VERSION`을 함께 변경하기 전에 실제 배포 번들을 검토하고 아래 회귀 테스트를 실행할 것. 기존 버전에서 도구 없는 beta 요청의 모델을 바꾸던 한 줄이 2.6.15에서 `smallModel` 지역 변수와 warmup 로그를 사용하는 블록으로 바뀌어 시작이 차단된 사례가 있음.

래퍼는 두 가지 알려진 구문만 정확히 인식. 알 수 없는 구문·중복 앵커·잔여 대입문·부분 패치는 명시적으로 거부하며, 검증을 끄고 시작하지 않음. 파일 교체는 원자적이고 두 번째 적용은 파일을 변경하지 않음. 이미 실행 중인 copilot-api에 버전 변경을 적용하려면 다른 세션이 사용하지 않는지 확인한 뒤 `./stop-proxy.sh copilot-api`와 `./start-proxy.sh` 실행.

## WebSearch 동작 방식 (2026-08 실검색 검증 / 2026-09-27 설정 확인)

Claude Code의 WebSearch는 Anthropic 서버가 실행하는 서버사이드 툴(`web_search_20250305`)이라 Copilot 백엔드로는 원래 불가능하지만, **copilot-api에 내장된 `messageApiWebSearchModel` 기능**이 이를 해결한다: web_search가 유일한 툴인 `/v1/messages` 요청을 감지하면 Responses 지원 GPT 모델의 Copilot `/responses`로 재라우팅하고, OpenAI의 네이티브 hosted `web_search` 툴(서버사이드 실검색, 외부 검색 API 키 불필요)을 실행한 뒤 결과를 Anthropic 네이티브 포맷(`server_tool_use` + `web_search_tool_result`)으로 재구성해 돌려준다. 2026-08에는 GPT-5-mini로 스트리밍 포함 실검색을 검증했으며, 이번 복구에서는 유료 추론·실검색을 다시 호출하지 않음.

**명시적 검색 보조 동작이며 일반 모델 대체와 구분.** Haiku 일반 요청은 실제 Haiku로 전달하지만 `web_search` 전용 요청은 설정된 검색 보조 모델이 실행. **2.6.15의 새 설치 기본값은 `gpt-6-luna`**이고, 현재 로컬 설정은 기존의 명시적 **`gpt-5-mini`**를 유지. 래퍼는 이 설정을 덮어쓰지 않음. 검색 응답의 `model`은 원래 요청 이름을 유지할 수 있으므로 실제 검색 실행 모델의 증거로 사용하지 않음.

요청은 반드시 copilot-api(:4141) 경유. 실제 Haiku와 GPT-5-mini 모두 해당 경로 유지. GPT-4o-mini가 picker에 다시 등장해도 같은 검색 경유 경로를 유지. LiteLLM `github_copilot` 직결 경로는 이 서버 툴 형식을 번역하지 못함.

## 알려진 제약

- **web_fetch 서버 툴**(`web_fetch_20250910`): 게이트웨이 전체에서 불가 — claude-* 경로는 Copilot이 400("rejected tool(s): web_fetch"), GPT 경로는 copilot-api 크래시(500). 단 Claude Code의 WebFetch 툴은 CLI가 직접 URL을 가져오는 클라이언트 실행 방식이라 실사용 영향 없음.
- **web_search + 다른 툴 혼합 요청**: copilot-api가 web_search를 조용히 제거(검색 미실행, 에러는 없음). Claude Code는 웹서치를 단독 요청으로 보내므로 실사용 무관. 혼합 + tool_choice로 web_search 강제 시엔 400.
- **count_tokens**: 로컬 근사치 — tools 배열은 토큰 계산에서 무시되므로 툴 많은 요청은 과소집계. Claude Code의 컨텍스트 추적 용도로는 충분.
- **github_copilot 직결 경로 usage**: input_tokens가 실제보다 크게 과소보고됨 (~1700토큰 프롬프트가 17로 찍힘) — 이 경로 모델들의 비용 집계는 신뢰 불가.
- **동시 요청 응답 혼선(1회 관측)**: 병렬 부하 중 opus 요청에 sonnet 응답이 온 사례 1회. 재현 안 됨. 여러 세션 동시 사용 중 엉뚱한 응답이 오면 이걸 의심할 것.
- **grok-4.5**: tools 없는 bare 요청은 copilot-api 버그(tool_choice without tools)로 400. Claude Code는 항상 tools를 보내므로 실사용 무관.
- **종료된 모델 ID**: 예전 `mai-code-1-flash`/`mai-code-1-flash-picker` 대신 현재 카탈로그에는 `mai-code-1.1-flash`가 공개됨. 종료된 ID를 새 모델로 자동 대체하지 않으므로 현재 목록의 실제 ID를 선택.
- **litellm github_copilot 네이티브 /v1/messages 경로**: "unknown Copilot-Integration-Id"로 깨져 있어(1.95.0) claude-*는 copilot-api 경유로 우회 중. 업스트림 수정 시 단순화 가능.
- **인증 오류 상태 코드**: 현재 DB 없는 LiteLLM 1.95.0은 마스터키 누락에 500, 잘못된 키에 400(`no_db_connection`)을 반환. 요청은 거부되지만 401로 정규화되지 않음. 올바른 마스터키의 discovery는 200.
- **비대화형 WSL 도구의 프로세스 수명**: 이번 검증 환경에서는 동기 도구 호출 종료 후 copilot-api가 종료됨(npx 없이 직접 실행해도 동일). readiness는 감독 프로세스를 유지한 상태에서 확인하며, 감독 세션을 닫은 뒤에는 로컬 WSL 셸에서 `ccp`를 다시 실행할 것. 패키지 시작 오류와 실행 호스트의 프로세스 수명 제약을 구분.
- **ToS**: 에디터 외부에서 Copilot API 사용은 GitHub 비공식 영역. 개인·로컬·수동 규모에선 관찰된 최악 사례가 "경고 메일 → Copilot 일시정지" 수준이지만, 대량/병렬 트래픽·외부 공유는 위험 가중. 로컬 단독 사용 유지 권장.

## 기능 검증 현황 (2026-08-05, 33개 테스트)

| 기능 | 상태 |
|---|---|
| 스트리밍 (SSE) | ✅ 전 라우트 (Patch 3a/3b 적용 후) |
| 툴콜 왕복 / 멀티툴 / 병렬 툴콜 | ✅ 전 라우트 |
| WebSearch (실검색) | ✅ capi 경로, 스트리밍 포함 |
| Extended thinking / interleaved | ✅ (budget_tokens는 Copilot adaptive로 변환 — 쉬운 질문엔 생각 생략, 정상) |
| 비전 (이미지 입력) | ✅ claude-*/gpt-5-mini/gemini (gpt-4o·gpt-4o-mini는 업스트림 카탈로그 제외로 불가) |
| 프롬프트 캐싱 | ✅ claude-* 실캐싱 (write→hit→증분), 기타 경로는 무해하게 수용/제거 |
| count_tokens, [1m] 변형, stop_sequences, metadata | ✅ |
| web_fetch 서버 툴 | ❌ (위 "알려진 제약" 참고, 실사용 영향 없음) |

## 재로그인이 필요한가?

아니오. GitHub OAuth device flow로 최초 1회 로그인하면 장수명(`ghu_*`) 토큰이 `~/.config/litellm/github_copilot/access-token`에 저장되고, 이후 짧은 수명(~25분)의 Copilot 베어러 토큰은 `refresh-models.sh`/`start-proxy.sh`가 그 장수명 토큰으로 자동 재발급한다. 토큰을 GitHub에서 직접 revoke하거나 파일을 지우지 않는 한 재로그인 불필요.

## 보안 설계

- 두 서비스(litellm :4000, copilot-api :4141) 모두 `127.0.0.1`에만 바인딩 — 외부 네트워크에서 접근 불가
- litellm 마스터 키는 `.env`에 랜덤 생성되어 저장 (하드코딩 없음), git에 커밋되지 않음
- GitHub 토큰은 프로세스 인자(`ps aux`로 노출)로 전달하지 않고 파일 스토어(`~/.local/share/copilot-api/github_token`, 600)로만 전달
- 크레덴셜 파일/디렉터리 권한 600/700로 제한
- `refresh-models.sh`는 fail-closed: Copilot API가 비정상 응답(모델 5개 미만)이면 기존 `config.yaml`을 덮어쓰지 않고 중단

## 이전 설정 마이그레이션

- `claude-fable-5`, `claude-mythos-5`, 종료된 Opus/Sonnet 4.6 이름의 타모델 대체 제거. 사용 중이면 실제 `claude-opus-5` / `claude-sonnet-5`를 명시적으로 선택.
- **`claude-haiku-4-5`는 이제 실제 Haiku 4.5.** GPT-5-mini를 원하면 `gpt-5-mini` 선택. 작은 모델 기본값과 실제 표시 이름을 혼동하지 않음.
- 런처는 저장된 기본값이 다른 모델로 덮어쓰지 않도록 `--model`을 명시. 사용자가 직접 전달한 `--model`과 환경변수는 보존.
- copilot-api의 도구 없는 beta 요청 자동 GPT 교체는 시작 래퍼에서 차단. `modelMappings` 또는 `claudeAutoModel`로 별도 대체가 설정되어 있으면 자동 변경하지 않고 시작 거부.
- LiteLLM 패치 또는 copilot-api 번들 앵커가 바뀌면 시작 중단. 업그레이드 후 패치가 적용된 척 진행하지 않음.

## 검증 명령

```bash
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  ~/.local/share/uv/tools/litellm/bin/python -m unittest discover -s tests -v
./list-models.sh
```

- 단위/통합 테스트: 실제 생성 스크립트와 임시 카탈로그, 실제 LiteLLM Router, 2.6.15 설치 번들의 임시 사본, 구·신 패치/거부/원자성/멱등성, 런처 인자·readiness·PID 한정 종료. 실제 토큰 대신 테스트 자격 증명을 쓰며 네트워크 추론 없이 실행.
- 운영 확인: 두 서비스의 `/v1/models`, 공개 ID·`upstream_model_id` 중복 없음, 모든 `display_name`·설명·한도 일치, 실제 패키지 버전과 모델 보호 패치 확인. Claude Code는 stub으로만 런처 경계를 확인하며 대화형 에이전트를 시작하지 않음.
- 주요 Claude/GPT 스트리밍과 WebSearch 실검색은 별도 유료 추론 스모크 테스트이므로 명시적으로 요청된 경우에만 수행.
- 응답 `model`은 요청 별칭을 그대로 돌려줄 수 있음. Claude 일반 응답의 실제 모델 검증에는 `copilot_usage.token_details[].model`도 대조.

## 크레덴셜

- `~/.config/litellm/github_copilot/access-token` — 장수명 GitHub OAuth 토큰 (600)
- `~/.config/litellm/github_copilot/api-key.json` — 단수명(~25분) Copilot 베어러, 자동 갱신 (600)
- `~/litellm-copilot-gateway/.env` — litellm 마스터키 (600, 랜덤 생성)
- copilot-api는 같은 GitHub 토큰을 재사용 (별도 인증 불필요)
