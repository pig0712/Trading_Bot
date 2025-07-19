# 📈 자동 매매 트레이딩 봇 (Auto Trading Bot)

이 프로젝트는 Gate.io 거래소의 무기한 선물 계약을 대상으로 하는 Python 기반 자동 매매 봇입니다. 사용자가 정의한 전략에 따라 자동으로 거래를 실행하며, 다양한 고급 기능을 포함하고 있습니다.

## ✨ 주요 기능

* **전략적 자금 관리**: 분할 매수(물타기) 및 피라미딩(불타기)을 통한 정교한 자금 운용
* **수익 극대화 전략**: 수익금 기준 추적 익절(Trailing Take-Profit) 기능
* **조건부 방향 결정**: 이동평균선(SMA)과 상대강도지수(RSI)를 결합한 추세 분석 또는 사용자 수동 지정
* **안전 장치**: API 통신 지연을 고려한 재진입 방지 로직 및 레버리지 설정 검증
* **편리한 인터페이스**: `click` 기반의 대화형 명령줄 인터페이스(CLI)
* **상세 로깅**: 일반 로그와 오류 로그를 분리하여 기록 및 관리

## 📂 프로젝트 파일 구조 및 설명

이 프로젝트는 다음과 같은 파일 및 디렉토리 구조를 가지고 있습니다.

.<br>
├── .gitignore                 # 🚫 Git 버전 관리에서 제외할 파일 목록 <br>
├── Trading_BOT/               # ▶️ 실제 봇 프로젝트의 메인 디렉토리 <br>
│   ├── .env                   # 🔑 (사용자 생성) API 키 등 민감 정보 저장<br>
│   ├── Bot/                   # 💾 저장된 매매 전략 설정 파일 (.json)<br>
│   ├── logs/                  # 📜 로그 파일 저장 디렉토리<br>
│   │   ├── trading_bot.log<br>
│   │   └── trading_bot_errors.log<br>
│   ├── main.py                # 🚀 봇 애플리케이션의 메인 진입점<br>
│   ├── pyproject.toml         # 🧱 프로젝트 설정 및 의존성 관리 (uv, poetry 등)<br>
│   ├── uv.lock                # 🔒 의존성 버전 고정 파일<br>
│   └── src/<br>
│       └── trading_bot/<br>
│           ├── init.py<br>
│           ├── cli.py         # 💻 CLI 및 메인 전략 실행 로직<br>
│           ├── config.py      # ⚙️ 봇 설정 관리 (BotConfig)<br>
│           ├── exchange_gateio.py # 🔗 Gate.io API 통신 클라이언트<br>
│           ├── liquidation.py # 💧 예상 청산가 계산<br>
│           └── prices.py      # 💹 외부 가격 조회 (보조 기능)<br>
└── ...<br>


---

### ### 📁 최상위 디렉토리

* **`Trading_BOT/`** ▶️
    * 봇의 핵심 로직과 설정, 로그 등 모든 관련 파일이 포함된 **메인 프로젝트 디렉토리**입니다. 봇을 실행하려면 이 디렉토리의 `main.py`를 실행해야 합니다.

### ### 📁 `Trading_BOT/` 디렉토리 내부

* **`.env`** (사용자 직접 생성) 🔑
    * Gate.io API 키, 시크릿 키 등 민감한 정보를 저장하는 파일입니다. `.gitignore`에 의해 버전 관리에서 제외됩니다.

* **`Bot/`** 💾
    * 사용자가 대화형 CLI를 통해 생성한 매매 전략 설정이 `.json` 파일 형태로 저장되는 곳입니다.

* **`logs/`** 📜
    * 봇 실행 중 발생하는 모든 활동이 기록됩니다.
    * `trading_bot.log`: 일반 정보, 경고, 오류 등 모든 로그가 기록됩니다.
    * `trading_bot_errors.log`: **오류(ERROR) 및 치명적 오류(CRITICAL)만** 따로 기록되어 문제 발생 시 원인 파악을 용이하게 합니다.

* **`main.py`** 🚀
    * 봇 애플리케이션을 실행하는 **메인 스크립트**입니다.
    * `.env` 파일 로드, 로깅 시스템 초기화 등 실행에 필요한 사전 준비를 하고 `cli.py`를 호출합니다.

* **`pyproject.toml`** & **`uv.lock`** 🧱
    * 프로젝트에 필요한 Python 라이브러리(의존성)를 관리하는 파일입니다. `uv`나 `poetry` 같은 최신 패키지 매니저가 사용합니다.
    * `uv install` 명령으로 모든 라이브러리를 정확한 버전에 맞춰 한 번에 설치할 수 있습니다.

* **`src/trading_bot/`**
    * 봇의 핵심 소스 코드가 위치하는 패키지 디렉토리입니다.

---

### ### 📁 `src/trading_bot/` 디렉토리 (핵심 로직)

* **`cli.py`** 💻
    * **봇의 두뇌이자 조종석**입니다.
    * `run_strategy` 함수를 통해 주기적으로 시장을 모니터링하고, 분할매수, 피라미딩, 추적 익절 등 정의된 전략에 따라 주문을 관리합니다.
    * `pretty_show_summary` 함수를 통해 실시간 포지션 상태를 보기 쉬운 UI로 화면에 출력합니다.
    * `prompt_config` 함수를 통해 사용자와 대화하며 새로운 매매 전략을 설정합니다.

* **`config.py`** ⚙️
    * `BotConfig` 데이터 클래스를 통해 봇 운영에 필요한 모든 설정(레버리지, 진입/청산 조건, 피라미딩 옵션 등)을 구조화하여 관리합니다.
    * 설정 객체를 JSON 파일로 저장(`save`)하거나 불러오는(`load`) 기능을 제공하며, 설정값의 유효성을 자동으로 검사합니다.

* **`exchange_gateio.py`** 🔗
    * `GateIOClient` 클래스를 통해 Gate.io 거래소 API와 통신하는 모든 기능을 담당합니다.
    * 주문 실행, 포지션 조회(단방향/양방향 모드 호환), 계좌 잔고 확인 등 모든 API 호출을 추상화하여 제공합니다.

* **`liquidation.py`** & **`prices.py`** 💧💹
    * 각각 예상 청산가 계산, 외부 API를 통한 가격 조회 등 보조적인 유틸리티 기능을 담당합니다.

## 🚀 시작하기

### 1. 환경 설정

1.  이 저장소를 복제(clone)합니다.
2.  `.env` 파일을 생성하고 아래 내용을 채웁니다.
    ```
    GATE_API_KEY="YOUR_API_KEY"
    GATE_API_SECRET="YOUR_API_SECRET"
    GATE_ENV="live"  # 실거래는 live, 테스트넷은 testnet
    LOG_LEVEL="INFO" # DEBUG, INFO, WARNING, ERROR
    ```
3.  필요한 라이브러리를 설치합니다.
    ```bash
    uv install
    ```

### 2. 봇 실행

터미널에서 `Trading_BOT` 디렉토리로 이동한 후, `main.py`를 실행합니다.
```bash
cd Trading_BOT
python main.py
```