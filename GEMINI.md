<!-- BEGIN CPROJECTS COMMON GUIDANCE -->
## 공통 인프라·기존 구현 우선
작업 시작 시 `C:\projects\_shared\docs\PROJECT_DEFAULTS.md`와 `C:\projects\_shared\docs\REUSE_CATALOG.md`를 읽고 이 프로젝트의 기존 지침·예외를 함께 적용한다.
새 프로젝트는 Cloudflare 정적 HTML/SSG 우선, 동적 서버와 DB/Auth는 기존 Oracle 및 그 안의 Supabase 활용이 기본이다. zub의 Supabase Cloud 등 기존 예외는 유지한다.
Supabase 스택·Docker·로그인·결제·AI·배포 코드를 새로 만들기 전에 기존 구현을 찾고 재사용 범위와 신규 구현 사유를 정리한다. 기존 앱을 이 원칙만으로 일괄 이전하지 않는다.
키는 `C:\projects\lib`에 보관하며 키 내용·비밀번호·토큰을 출력하거나 문서/Git에 넣지 않는다.
공통 파일에 접근할 수 없으면 이 요약과 프로젝트 지침으로 진행하되, 실제 재사용 판단 전에 공통 문서 접근 문제를 알린다. 사용자의 최신 명시적 요청이 우선한다.
<!-- END CPROJECTS COMMON GUIDANCE -->
