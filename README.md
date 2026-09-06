# GitHub Copilot → Claude Code 게이트웨이

GitHub Copilot 구독의 모델들(Claude, GPT-5.x, Gemini, Grok)을 Claude Code에서 쓰기 위한 로컬 전용(127.0.0.1) 게이트웨이. LiteLLM이 Claude Code용 Anthropic 호환 엔드포인트를 노출하고, 내부적으로 GitHub Copilot API로 요청을 라우팅한다.

## 설치 (처음 셋업 / 다른 macOS로 이전 시)

### 0. 사전 준비
- **GitHub Copilot 구독**이 있는 GitHub 계정 (Individual/Business 무관, 이 저장소는 인증 방법을 다루지 않음 — 아래 2단계에서 이 리포와 무관하게 최초 1회 device-flow 로그인만 필요)
- macOS + `zsh`/`bash`
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
./apply-patches.sh     # 로컬 패치 3종 적용 (device-login 창, tool_choice, SSE 스트리밍 — "스크립트" 표 참고)
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
Claude Code ──/v1/messages──▶ litellm :4000 (127.0.0.1, 인증: .env의 마스터키)
                                │
                                ├─ claude-* ──────────────┐
                                ├─ responses 전용(gpt-5.x, │   anthropic/<id>
                                │   codex, grok, mai) ────┤──▶ copilot-api :4141
                                ├─ gpt-4o-mini,           │      ├─ claude-* → Copilot 네이티브 /v1/messages (thinking 보존)
                                │   haiku 별칭 ───────────┘      ├─ web_search 단독 요청 → GPT /responses 재라우팅 (실검색)
                                │                                └─ 그 외    → Copilot /responses (자동 번역)
                                └─ 일반 chat 모델(gemini,
                                    gpt-4o/4.1/3.5) ──────▶ github_copilot provider → Copilot /chat/completions
```

- **litellm 1.95.0** + **fastapi==0.140.6** (0.140.7+는 `get_flat_dependant` 제거로 부팅 실패 — litellm#35763)
- **copilot-api** = `@jeffreycao/copilot-api` (caozhiyuan/copilot-api) — responses 전용 모델 번역 담당
- 모든 비-claude 모델에는 `claude-<이름>` 별칭이 있음 (Claude Code `/model` 피커가 claude*/anthropic* ID만 표시하기 때문)

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

- 세션 안: `/model`로 전환 (피커의 `claude-gpt-...` 별칭 = 해당 GPT 모델)
- 평소 `claude` 명령(진짜 Anthropic)은 영향 없음 — 환경변수는 이 스크립트의 자식 프로세스에만 적용
- 기본값: main=`claude-sonnet-5`, opus=`claude-opus-5`, haiku/small-fast=`gpt-5-mini` (gpt-4o-mini는 Copilot 카탈로그에서 빠진 레거시 ID — 텍스트는 되지만 이미지 요청이 업스트림 400이라 승격함. gpt-5-mini는 비전·웹서치·스트리밍 모두 정상)
- `claude-copilot.sh`는 항상 `--dangerously-skip-permissions`로 실행됨 (권한 프롬프트 없이 자동 승인 — Copilot 백엔드 전용 로컬 세션이므로 실제 Anthropic `claude` 명령에는 영향 없음)

## 스크립트

| 파일 | 역할 |
|---|---|
| `start-proxy.sh` | copilot-api(:4141) + litellm(:4000) 기동, 둘 다 127.0.0.1 전용 |
| `stop-proxy.sh` / `restart-proxy.sh` | 정지 / litellm만 재시작(설정 리로드) |
| `claude-copilot.sh` | Claude Code 런처 (게이트웨이 자동 기동 + 실행마다 모델 목록 자동 refresh 포함) |
| `refresh-models.sh` | Copilot 실시간 목록으로 config.yaml 재생성 (fail-closed, 백업 후 원자적 교체) |
| `list-models.sh` | 현재 Copilot 모델 + 엔드포인트 + 컨텍스트 크기 조회 |
| `apply-patches.sh` | litellm 업그레이드 후 로컬 패치 재적용 (Patch 1: device-login 창 1분→10분, Patch 2: 빈 tools에 남은 tool_choice 제거, Patch 3a/3b: SSE 스트리밍 빈 choices 청크 가드) |

## 새 모델이 나오면

`claude-copilot.sh`(`ccp`)는 실행할 때마다 자동으로 아래를 먼저 돌리므로, 평소엔 아무것도 안 해도 새 모델이 반영된다:

```bash
~/litellm-copilot-gateway/refresh-models.sh && ~/litellm-copilot-gateway/restart-proxy.sh
```
목록에 없어도 와일드카드(`github_copilot/*`)가 chat 호환 모델은 즉시 처리하지만, **responses 전용 신모델(gpt-6-astra 같은)은 와일드카드가 못 받는다** — capi 라우팅이 필요해서 refresh 전엔 아예 안 됨. Claude Code를 거치지 않고 게이트웨이만 갱신하고 싶을 때 위 명령을 수동으로 돌려도 된다.

## litellm 업그레이드 시 (주의)

```bash
uv tool install --python 3.13 'litellm[proxy]==<ver>' --with 'fastapi==0.140.6'
~/litellm-copilot-gateway/apply-patches.sh     # 패치 재적용 필수
~/litellm-copilot-gateway/restart-proxy.sh
```
fastapi 핀은 litellm#35763 (PR #35389/#35139/#35773/#35858 중 하나) 머지 후 제거 가능.

## WebSearch 동작 방식 (2026-08 검증)

Claude Code의 WebSearch는 Anthropic 서버가 실행하는 서버사이드 툴(`web_search_20250305`)이라 Copilot 백엔드로는 원래 불가능하지만, **copilot-api에 내장된 `messageApiWebSearchModel` 기능**(기본값 `gpt-5-mini`, config 불필요)이 이를 해결한다: web_search가 유일한 툴인 `/v1/messages` 요청을 감지하면 Responses 지원 GPT 모델의 Copilot `/responses`로 재라우팅하고, OpenAI의 네이티브 hosted `web_search` 툴(서버사이드 실검색, 외부 검색 API 키 불필요)을 실행한 뒤 결과를 Anthropic 네이티브 포맷(`server_tool_use` + `web_search_tool_result`)으로 재구성해 돌려준다. 스트리밍 포함 동작 검증 완료.

**단, 요청이 copilot-api(:4141)를 거쳐야만 동작한다.** litellm의 github_copilot 직결 경로는 이 툴 형태를 번역하지 못해 400("tools are required when tool choice is specified")이 나므로, WebSearch가 흘러가는 small/fast 모델(`gpt-4o-mini`, haiku 별칭)은 반드시 capi 라우팅을 유지할 것 (`refresh-models.sh`가 처리).

## 알려진 제약

- **web_fetch 서버 툴**(`web_fetch_20250910`): 게이트웨이 전체에서 불가 — claude-* 경로는 Copilot이 400("rejected tool(s): web_fetch"), GPT 경로는 copilot-api 크래시(500). 단 Claude Code의 WebFetch 툴은 CLI가 직접 URL을 가져오는 클라이언트 실행 방식이라 실사용 영향 없음.
- **web_search + 다른 툴 혼합 요청**: copilot-api가 web_search를 조용히 제거(검색 미실행, 에러는 없음). Claude Code는 웹서치를 단독 요청으로 보내므로 실사용 무관. 혼합 + tool_choice로 web_search 강제 시엔 400.
- **count_tokens**: 로컬 근사치 — tools 배열은 토큰 계산에서 무시되므로 툴 많은 요청은 과소집계. Claude Code의 컨텍스트 추적 용도로는 충분.
- **github_copilot 직결 경로 usage**: input_tokens가 실제보다 크게 과소보고됨 (~1700토큰 프롬프트가 17로 찍힘) — 이 경로 모델들의 비용 집계는 신뢰 불가.
- **동시 요청 응답 혼선(1회 관측)**: 병렬 부하 중 opus 요청에 sonnet 응답이 온 사례 1회. 재현 안 됨. 여러 세션 동시 사용 중 엉뚱한 응답이 오면 이걸 의심할 것.
- **grok-4.5**: tools 없는 bare 요청은 copilot-api 버그(tool_choice without tools)로 400. Claude Code는 항상 tools를 보내므로 실사용 무관.
- **mai-code-1-flash**: Copilot 쪽에서 완전히 죽어있음(양쪽 400). `mai-code-1-flash-picker`를 쓸 것 (config에서 bare 이름은 제외됨).
- **litellm github_copilot 네이티브 /v1/messages 경로**: "unknown Copilot-Integration-Id"로 깨져 있어(1.95.0) claude-*는 copilot-api 경유로 우회 중. 업스트림 수정 시 단순화 가능.
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

## 알려진 이슈 대응 사례

- **`claude-fable-5` 등 저장된 기본 모델과 충돌**: Claude Code `/model`로 저장한 기본 모델(예: Fable 5)이 `claude-copilot.sh` 실행 시 게이트웨이의 `ANTHROPIC_MODEL`을 덮어써서 존재하지 않는 Copilot 모델을 요청 → "unknown Copilot-Integration-Id" 에러. 대응: (1) 실제 Anthropic 모델명 → Copilot 모델 별칭을 `refresh-models.sh`에 추가, (2) `claude-copilot.sh`가 `--model`을 명시적으로 넘기지 않는 한 항상 `$ANTHROPIC_MODEL`을 강제 지정하도록 수정.
- **Copilot이 dated Claude id를 retire**: 2026-09-07, `claude-opus-4.6`/`claude-sonnet-4.6`이 Copilot 카탈로그에서 사라짐 → `refresh-models.sh`에 리터럴로 박혀있던 호환 별칭(`claude-opus-4-5`/`claude-opus-4-1`/`claude-sonnet-4-5`/`claude-sonnet-4-6`)이 죽은 upstream id를 계속 가리켜 `model_not_supported` 400 발생. `refresh-models.sh` 재실행만으론 안 고쳐짐(동적 카탈로그 루프는 살아있는 id만 반영, 리터럴 별칭은 스크립트 소스를 직접 고쳐야 함). 대응: 별칭 타겟을 `claude-opus-5`/`claude-sonnet-5`로 갱신, 그리고 dated id 자체(`claude-opus-4.6` 등)도 호환 별칭으로 남겨 리다이렉트하도록 추가.

## 크레덴셜

- `~/.config/litellm/github_copilot/access-token` — 장수명 GitHub OAuth 토큰 (600)
- `~/.config/litellm/github_copilot/api-key.json` — 단수명(~25분) Copilot 베어러, 자동 갱신 (600)
- `~/litellm-copilot-gateway/.env` — litellm 마스터키 (600, 랜덤 생성)
- copilot-api는 같은 GitHub 토큰을 재사용 (별도 인증 불필요)
