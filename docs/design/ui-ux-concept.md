# Concept UI/UX — compilare cerințe + mecanism de garanție (LIVING DOC)

**Status:** în construcție (pornit 22-06-2026 din feedback-ul fondatorului; mecanismul §4 definit
după cele două cercetări — vezi §6). Sursa canonică pentru reproiectarea modului în care fabrica
generează UI/UX. Acoperă cererea fondatorului #10 (compilarea schimbărilor de UX + garanția la
nivel de execuție).

## 0. Problema (de ce facem asta)
UI iese funcțional dar „fără gust", inconsistent între ecrane; culori/spațiere alese prost.
Diagnostic confirmat de cercetare: avem ȚINTELE (strat de componente proprii, un fișier de
tokens) dar fără (a) APLICARE mecanică, (b) spec pentru ASPECT, (c) nimic care să VADĂ rezultatul,
(d) un judecător uman de gust. „Fără gust / inconsistent" e rezultatul TIPIC și previzibil al
acestei combinații — nu o defecțiune punctuală.

## 1. Cerințe & schimbări dorite (feedback fondator 22-06)
### A. Date & gestionare entități (blochează testarea)
- Lipsește gestionarea datelor de bază în aplicație: contragenți, contracte, marfă au MODELE
  (modulul `parties` etc.) dar NU au ecran/API de adăugare-editare-ștergere; doar panoul tehnic
  Django. Presupunere de plan: datele vin din migrarea 1C (neconstruită). [#1, #2 + întrebarea]
- Oriunde se selectează o entitate existentă → buton „adaugă nouă" pe loc. [#3] → §3.
- Populare cu date demo pentru testare. [#11] → în lucru (seed demo).
### B. Calitatea & procesul de generare UI
- Culori (font/fundal) de refăcut cu gust. [#4]
- UI „fără gust" → instrumente AI + metodologii, ce adaptăm. [#7] → cercetat (§6).
- Chestionar UX obligatoriu înainte de build. [#9] → §2.
- Compilarea + garanția la execuție. [#10] → acest document + §4.
### C. Arhitectura & modularitatea UI
- Organizarea meniului/navigării modulară, ușor de regândit (submeniu/taburi/orizontal/view-uri
  per flux). [#5]
- Separare backend/frontend pe ETAPE diferite; frontul modificabil cu efort/risc minim. [#6] → §3/§4.
- UI parametrizabil — CE EXISTĂ: ~16 componente proprii (singurul loc care atinge biblioteca de
  bază), tokens centralizați, preferințe de view. CE LIPSEȘTE: paletă cu gust + aplicarea mecanică
  a regulilor + parametrizare meniu. [#8]

## 2. Chestionar UX obligatoriu — poarta de spec (#9)
→ MUTAT în `work-protocols/ui-ux-laws.md` (legea de fabrică injectată pe etapele de frontend). Vezi ui-ux-laws.md §2.

## 3. Principii/reguli globale de UI
→ MUTAT în `work-protocols/ui-ux-laws.md`. Vezi ui-ux-laws.md §3.

## 4. Mecanismul de garanție la nivel de execuție (#10) — DEFINIT (ordine = pârghie)
→ MUTAT în `work-protocols/ui-ux-laws.md`. Vezi ui-ux-laws.md §4.

## 5. Decizii (rezolvate 22-06-2026)
1. **Referința vizuală:** ✅ NetSuite + Dynamics 365 + Odoo + filosofia UX-ERP a fondatorului → `work-protocols/ui-ux-laws.md §7`.
2. **Gestionare date de bază:** ✅ **ECRANE în aplicație** (adăugare/editare/ștergere), nu doar import 1C + panoul Django.
3. **Mecanismele §4 + separarea front/back:** ✅ **adoptate**, cu 2 ajustări (în `ui-ux-laws.md §4`): captură la **2 lățimi** (desktop + telefon, fără tabletă); poarta vizuală a fondatorului **doar la primele câteva iterații, apoi reevaluăm**.
4. **Cadența reviziei:** ✅ fondatorul vede ecranele importante la fiecare fază de UI.
5. **Accesibilitate AA implicit:** ✅ DA.
6. **Paleta de culori:** ⏳ se decide după ce se setează regulile; până atunci stil modern default (Claude Code/codex). Nu e critic.

## 6. Intrări (surse)
- Cercetare „cum se face UI/UX cu AI" (instrumente/metodologii/integrare): `docs/research/ui-ux-ai-assisted-research-22-06-2026.md` ✅
- Cercetare „probleme tipice AI-frontend + contramăsuri": `docs/research/ai-frontend-pitfalls-22-06-2026.md` ✅
- Fundația UI existentă: contractul F6 (componente proprii, tokens, legea UX-first).

---

## 7. Referință vizuală + filosofia UX — INTRAREA FONDATORULUI (22-06; decizia §5.1 REZOLVATĂ)
→ MUTAT în `work-protocols/ui-ux-laws.md` (aplicații-referință + principiile fondatorului + cele 4 legi
inviolabile + deciziile de design + checklist-ul + sinteza arhitect). Vezi ui-ux-laws.md §7.
