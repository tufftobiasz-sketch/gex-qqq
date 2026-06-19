# GEX-QQQ — Gamma Exposure Dashboard

Osobisty dashboard pokazujący GEX/Gamma dla QQQ: Call Wall, Put Wall, Inflection (gamma flip)
oraz typ dnia (Trend / Balance).

## Uruchomienie lokalne (na komputerze)

```bash
cd C:\Users\tobis\Downloads\GEX-QQQ
pip install -r requirements.txt
python app.py
```

Otwórz w przeglądarce: http://localhost:5000

## Hosting (telefon + komputer, dostęp z internetu)

1. Wrzuć folder na GitHub (instrukcja niżej).
2. Wejdź na https://render.com → New → Web Service → wybierz repo.
3. Render sam wykryje `render.yaml`. Kliknij Deploy.
4. Dostaniesz link `https://gex-qqq.onrender.com` — działa na każdym urządzeniu.

### Wrzucenie na GitHub

```bash
cd C:\Users\tobis\Downloads\GEX-QQQ
git init
git add .
git commit -m "GEX QQQ dashboard"
git branch -M main
git remote add origin https://github.com/TWOJA-NAZWA/gex-qqq.git
git push -u origin main
```

## Uwagi
- Dane z yfinance: ~15 min opóźnienia, Open Interest aktualizowany raz dziennie.
- Darmowy plan Render usypia po 15 min bezczynności — pierwsze wejście może trwać ~30 s.
