# 2027 calendar (manual seed)

`calendar_2027.csv` holds the 2027 calendar as announced on 16 September 2026, because no
API (Jolpica, OpenF1) publishes it yet. Session start times are not included; they come from
Jolpica once the season appears there.

**Rule:** as soon as Jolpica's 2027 schedule is non-empty, the pipeline uses Jolpica and this
file is ignored. Daily schedule snapshots in bronze record any later changes (e.g. a race
moved because of the situation in the Middle East).

Sources, cross-checked on 2026-11-24 (24 rounds, identical order, dates and sprint list):
- https://www.racefans.net/2027-f1-season/2027-f1-calendar/ (announced 16 Sep 2026)
- https://en.wikipedia.org/wiki/2027_Formula_One_World_Championship

`circuit_ref` uses Jolpica's circuit reference ids.
