# Klik-snapshot

Maakt bij elke muisklik een PNG-screenshot. Volledig offline (X11, Cinnamon).

Starten: snelkoppeling **Klik-snapshot** op het bureaublad/in het menu, of
`python3 klik_snapshot.py`.

## Modi
- **Actieve applicatie** – het venster waarop je klikt (incl. titelbalk)
- **Scherm waarop geklikt wordt** – de monitor onder de muis
- **Alle schermen** – het volledige bureaublad
- **Geselecteerd gebied** – via "Selecteer gebied…" een rechthoek slepen (Esc = annuleren)

## Opties
- Opslagmap (wordt aangemaakt als die niet bestaat); bestanden heten `snap_JJJJMMDD_UUMMSS_mmm.png`
- Welke muisknoppen een snap maken (links/midden/rechts)
- Vertraging na klik in ms (0 = toestand op het moment van klikken; bijv. 300 om het resultaat van de klik te zien)
- Klikpositie markeren met een rode cirkel

Klikken in het eigen venster worden genegeerd. Instellingen staan in
`~/.config/klik-snapshot/config.json`.

## Afhankelijkheden
Alleen systeem-Python-pakketten: `python3-tk`, `python3-pil`, `python3-xlib`.
Werkt alleen onder X11 (niet Wayland).
