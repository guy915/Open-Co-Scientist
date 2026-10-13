# Open Co-Scientist security policy

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/guy915/Open-Co-Scientist/security/advisories/new).
You can also open the repository's **Security** tab and select
**Report a vulnerability**. Do not post vulnerabilities in public issues,
pull requests or discussions.

Include the affected component and revision, impact, steps to reproduce, and
any suggested fix. Use a small proof of concept against your own local
checkout. Do not include real credentials, other users' data or a production
database.

## Scope and supported versions

Security fixes cover the latest commit on `main` and the current hosted
Open Co-Scientist deployment at https://open-coscientist.com. There are no
maintained release branches.

In scope:

- API authentication, session and ownership checks.
- Provider credential handling and private research data.
- Reference MCP server URL guards and retrieval boundaries.
- The sandbox for model-written programs, including disabling execution when
  confinement is unavailable.
- Vulnerabilities in dependencies as used by this project.

Report flaws in model providers, data sources and other upstream services to
those services. For unmodified third-party code, also notify its upstream
maintainer; see [NOTICE](NOTICE). Tell us privately if our use is affected.
Findings that require an already compromised account, browser or machine,
volume-only denial of service, and scanner output without demonstrated impact
are outside this policy's scope.

## Response and disclosure

This project has one maintainer. The maintainer aims to acknowledge reports
within 7 calendar days and provide an initial assessment within 14 calendar
days. While a confirmed issue remains open, the aim is to send an update at
least every 14 calendar days. These are targets, not guaranteed deadlines.
There is no guaranteed fix date or round-the-clock response service.

The maintainer coordinates a fix and disclosure with the reporter. Please
allow reasonable time to assess and fix the issue before public disclosure.
Reports can be credited in the advisory with the reporter's consent.

There is no bug bounty or paid reward program.

## Good-faith research

Test locally where possible. On the hosted service, use only your own account
and data. Do not access other users' data, change their data, or disrupt their
work. Stop and report privately if you encounter someone else's data.

We will not pursue or support legal action against good-faith research that
follows this policy. This commitment applies to this project; it cannot bind
third-party services.
