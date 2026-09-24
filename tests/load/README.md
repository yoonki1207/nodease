# Nodease 로컬 셀프호스팅 부하 테스트

이 디렉토리는 Docker Compose로 실행한 Nodease를 현재 Mac에서 검증하는 Locust 및 단계별 HTTP 부하 도구다. 기본 시나리오는 외부 LLM, RAG, HTTP, Sandbox 호출이 없는 `startNode → templateNode → answerNode`를 사용하고, 한계 탐색 시험은 전용 HTTP mock으로 10초 I/O 대기를 재현한다.

이 결과는 로컬 엔지니어링 기준선이며 엔터프라이즈 운영 용량이나 SLA를 뜻하지 않는다. 첫 실행은 탐색적 기준선이고, 측정 결과를 본 뒤 p95/p99·오류율 기준을 확정한 두 번째 실행부터 합격/불합격을 판정한다.

실패 지점까지 부하를 높이는 시험은 [실패 한계 탐색 계획](BREAKPOINT_TEST_PLAN.md)에 정리했다. 당시 준비 절차와 수동 단계 실행·복구 상한은 [실행 기록과 단계별 명령](BREAKPOINT_RUNBOOK.md)에 남겼다. [2026-09-22 실행 결과](results/2026-09-22-breakpoint/summary.md)에서는 3.75 RPS가 통과했고, 4.375 RPS의 DB 연결 고갈·실행 기록 손실이 10분 안에 회복되지 않아 후속 시험을 중단했다. 일반 환경은 복구했다. 이번 결과는 단일 회차이며 운영 용량을 확정하지 않는다.

## 기준 워크로드

| 시나리오 | 부하 | 목적 |
| --- | ---: | --- |
| 고객 데이터 | 등록 사용자/active membership 200명 | 엔터프라이즈 tenant의 기본 cardinality |
| UI | 로그인 사용자 25명 | 앱/워크플로 조회, draft CAS 저장, 실행, 최근 run 조회 |
| API average | 0.35 request-start RPS, worker 6명 | 업무일 10,000회 수준의 평균 도착률 |
| API peak | 1 request-start RPS, worker 15명 | 예상 피크 |
| API burst | 3 request-start RPS, worker 45명 | 1분 수준의 짧은 burst |
| Mixed | UI 25명 + 선택한 API profile | 제어 평면과 실행 평면의 동시 부하 |

API worker 수는 `ceil(target RPS × 10초 latency budget × 1.5 headroom)`이다. Locust user는 요청을 겹쳐 실행하지 않으므로 이 시나리오는 진짜 open model이 아니라 bounded arrival-rate approximation이다. 실제 request-start rate가 목표의 95% 미만이면 서버 성능 결론을 내리지 않고 해당 실행을 `invalid`로 처리한다.

현재 기준 Mac은 Apple M3 8-core/16 GiB이고 Docker Desktop 할당은 8 CPU/약 7.7 GiB다. 같은 Mac에서 서비스와 Locust를 함께 실행하므로 결과에는 부하 발생기 자원 경쟁이 포함된다.

## 1. Locust 설치

앱 의존성과 분리된 가상환경을 사용한다. 설치 단계에서는 패키지 다운로드를 위해 네트워크가 필요할 수 있지만, 이후 provider-free 부하 요청은 외부 시스템을 호출하지 않는다.

```bash
python3.11 -m venv tests/load/.venv
tests/load/.venv/bin/pip install -r tests/load/requirements.txt
```

## 2. 격리된 Compose 환경 준비

Compose 파일의 고정 container name 때문에 개발 stack과 부하 테스트 stack을 동시에 실행할 수 없다. 먼저 `scripts/dev.sh`를 정상 종료한다. 아래 관리자는 프로젝트명을 항상 `nodease-loadtest`로, Docker context를 이 Mac의 `default`로 고정하며 개발용 named volume을 사용하지 않는다.

```bash
apps/gateway/.venv/bin/python tests/load/manage_compose.py init
apps/gateway/.venv/bin/python tests/load/manage_compose.py doctor
apps/gateway/.venv/bin/python tests/load/manage_compose.py up
```

`init`은 다음 두 gitignored 파일 중 첫 번째 파일에 필요한 local-only secret을 mode `0600`으로 생성한다. 이미 존재하면 덮어쓰지 않는다.

- `tests/load/.env.load.local`: Compose와 seed용 local secret
- `tests/load/.env.runtime.local.json`: seed 성공 후 생성되는 Locust 계정/ID manifest

부하 테스트 전용 Compose override는 PostgreSQL·Redis·Sandbox의 host port를 제거하고 Nginx만 전용 `load-ingress` bridge에 연결해 `http://127.0.0.1:18080`으로 공개한다. 애플리케이션의 내부 `moduly-network` 격리는 유지하면서 macOS Docker Desktop이 host forwarding을 만들 수 있게 하고, privileged host port 80도 피한다. Gateway 8000을 직접 target으로 사용하지 않는다. 관리자는 loopback이 아닌 host와 URL userinfo를 거부한다.

`doctor`가 `project=docker` 같은 foreign owner를 보고하면 해당 프로젝트를 원래 실행한 방식으로 정상 종료한 뒤 다시 실행한다. 관리자는 다른 프로젝트의 container를 자동 삭제하거나 인수하지 않는다. `.nodease-dev.lock`이 남아 있거나 dev container가 실행 중이면 `up`도 시작 전에 실패한다.

## 3. 25개 UI 계정과 provider-free workflow 생성

```bash
apps/gateway/.venv/bin/python tests/load/manage_compose.py seed
apps/gateway/.venv/bin/python tests/load/manage_compose.py verify
```

Seed는 다음 데이터를 한 transaction으로 멱등 upsert한다.

- load-test Organization 1개
- active 사용자와 membership 200개
- 사용자별 UI App/Workflow/direct manager permission 25세트
- 별도 API App/Workflow/Deployment/direct manager permission 1세트

사용자 비밀번호 hash와 API token verifier가 기존 secret과 일치하면 그대로 보존한다. 일치하지 않으면 자동 회전하지 않고 실패한다. API App에는 raw token을 저장하지 않으며 CLI·Locust 오류·리포트에도 password/token/응답 본문을 출력하지 않는다.

`verify`는 UI/API workflow graph와 active deployment snapshot이 canonical provider-free graph와 정확히 같은지도 확인한다. 하나라도 변조되면 외부 호출 가능성을 추정하지 않고 테스트 시작 전에 fail-closed 한다.

## 4. 기능 smoke

부하 전에는 고유 입력이 그대로 반환되고 `run_id`가 생성되는지 먼저 확인한다.

```bash
tests/load/.venv/bin/python -m locust \
  -f tests/load/smoke_test.py \
  --host http://127.0.0.1:18080 \
  --headless --users 1 --spawn-rate 1 --run-time 30s
```

HTTP 200만 확인하지 않고, 응답의 `status`, `run_id`, 최종 `answer_text`가 `Nodease load response: <고유 입력>`과 정확히 같은지 함께 검증한다. 모든 요청은 ambient proxy를 사용하지 않고 redirect를 따르지 않으며 20초 안에 응답하지 않으면 실패로 기록한다. 분산/multi-process Locust는 계정 배정과 request-start 측정 계약이 달라지므로 이 로컬 도구에서는 차단한다.

## 5. 시나리오별 실행

UI만 실행:

```bash
tests/load/.venv/bin/python -m tests.load.run_scenario ui --run-time 5m
```

API average, peak, burst를 각각 실행:

```bash
tests/load/.venv/bin/python -m tests.load.run_scenario api --api-profile average --run-time 5m
tests/load/.venv/bin/python -m tests.load.run_scenario api --api-profile peak --run-time 5m
tests/load/.venv/bin/python -m tests.load.run_scenario api --api-profile burst --run-time 1m
```

Mixed를 profile별로 실행:

```bash
tests/load/.venv/bin/python -m tests.load.run_scenario mixed --api-profile average --run-time 5m
tests/load/.venv/bin/python -m tests.load.run_scenario mixed --api-profile peak --run-time 5m
tests/load/.venv/bin/python -m tests.load.run_scenario mixed --api-profile burst --run-time 1m
```

각 실행은 `tests/load/reports/<UTC timestamp>_<scenario>_<profile>/`에 다음을 남긴다.

- `run_metadata.json`: workload, 사용자 수, target/achieved request-start RPS, arrival classification, 머신 정보, exit code
- `loadgen_result.json`: child Locust가 원자적으로 기록한 bounded arrival-rate 측정값
- `locust_stats.csv`, `locust_stats_history.csv`, `locust_failures.csv`
- `report.html`

UI-only 실행의 loadgen 필드는 명시적으로 `null`이다. API가 포함된 실행에서 loadgen artifact가 누락되거나 schema가 변조되면 결과를 `invalid_or_failed`로 처리한다.

동적 UUID, slug, email을 metric name에 넣지 않으므로 실행 간 통계를 직접 비교할 수 있다.

## 6. Queue와 Docker 자원 관측

별도 터미널에서 workflow queue를 CSV로 기록한다.

```bash
apps/gateway/.venv/bin/python tests/load/monitor_queue.py
```

동시에 Docker Desktop 또는 다음 명령으로 container CPU, memory, OOM/restart를 확인한다.

```bash
docker stats
```

첫 탐색 실행의 안전 중단 신호는 다음과 같다.

- OOM, container restart, migration/runtime crash
- 오류율이 1분 이상 1%를 초과
- workflow queue가 1분 이상 계속 증가하고 회복하지 않음
- provider-free execute p95가 10초를 넘으며 계속 악화
- API request-start rate가 목표의 95%에 못 미침(테스트 무효)

앞의 네 항목은 첫 실행용 안전 신호이지 아직 제품 SLO가 아니다.

## 7. 종료와 데이터 초기화

일반 종료는 container/network만 내리고 `nodease-loadtest_*` volume을 보존한다. `-v` 또는 `--volumes`를 사용하지 않는다.

```bash
apps/gateway/.venv/bin/python tests/load/manage_compose.py down
```

독립된 새 실험을 위해 load-test DB/Redis만 완전히 버릴 때에만 정확한 확인 문자열을 사용한다. 이 명령은 개발 volume에는 접근하지 않지만 load-test 실행 이력은 복구할 수 없다.

```bash
apps/gateway/.venv/bin/python tests/load/manage_compose.py \
  destroy-data --confirm nodease-loadtest
```

그 뒤 `up → seed → verify → smoke` 순서로 다시 준비한다. 개발 환경은 기존 방식으로 재시작하면 원래 개발 volume을 다시 사용한다.

## 결과 해석 순서

1. smoke correctness가 통과했는지 확인한다.
2. Locust exit code와 API arrival classification이 valid인지 확인한다.
3. 오류율, p50/p95/p99, throughput을 본다.
4. queue 증가와 container CPU/memory/restart를 같은 시간축으로 대조한다.
5. UI/API/mixed 중 어느 경로에서 병목이 먼저 나타나는지 기록한다.
6. 이 기준선으로 acceptance threshold를 정하고 동일 조건에서 한 번 더 실행한다.

기존 `load1.py`~`load3.py`, `run_benchmark.py`, `HISTORY.md`는 과거 결과 재현용 legacy 도구다. 새 기준선에는 사용하지 않는다.

## Kubernetes + Helm으로 옮길 때

Helm에서는 이 Compose 관리자를 사용하지 않는다. 같은 graph/manifest 계약을 유지하되 seed를 일회성 Job 또는 Gateway pod의 명시적 command로 실행하고, Locust는 cluster 밖의 별도 부하 발생기에서 Ingress를 target으로 실행한다. Gateway/Workflow worker/Log worker replica와 resource limit, 외부 PostgreSQL connection budget, Redis queue/memory, Ingress/TLS 조건을 결과 metadata에 고정해야 한다. HPA가 실제 chart에 연결되어 있는지도 별도로 검증한다.
