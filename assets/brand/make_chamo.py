#!/usr/bin/env python3
"""Generates the Chamo brand artwork (app icon, small icon, mascot) as SVG.

All shapes are drawn in a 120-unit design space and scaled, so the icon, the
small icon and the mascot share exact geometry. Run: python3 make_chamo.py
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent

# Palette: mint = the original format, amber = the converted format.
GREEN_TOP, GREEN_BOTTOM = "#22B083", "#0E7356"
MINT, AMBER, AMBER_DARK = "#A8EBD3", "#FFC85C", "#E8961E"
INK = "#0B4D3B"          # legs, crest, outlines
PUPIL = "#3B2500"

PAGE = "M30 47 H74 L90 63 V104 H30 Z"


def defs(plate=True):
    return f'''<defs>
    <linearGradient id="plate" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{GREEN_TOP}"/><stop offset="1" stop-color="{GREEN_BOTTOM}"/></linearGradient>
    <linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0.18"/><stop offset="0.55" stop-color="#fff" stop-opacity="0"/></linearGradient>
    <clipPath id="page"><path d="{PAGE}"/></clipPath>
    <clipPath id="front"><rect x="60" y="0" width="60" height="120"/></clipPath>
  </defs>'''


def document(edge="#fff"):
    """A page whose lower part has already turned into the new format."""
    return f'''<path d="{PAGE}" fill="#fff" stroke="{edge}" stroke-width="3" stroke-linejoin="round"/>
  <g clip-path="url(#page)"><rect x="20" y="82" width="80" height="30" fill="{AMBER}"/></g>
  <path d="M74 47 V63 H90 Z" fill="{MINT}" stroke="{MINT}" stroke-width="2" stroke-linejoin="round"/>
  <rect x="38" y="64" width="32" height="4" rx="2" fill="{MINT}"/>
  <rect x="38" y="72" width="24" height="4" rx="2" fill="{MINT}"/>
  <rect x="38" y="88" width="34" height="4" rx="2" fill="{AMBER_DARK}"/>
  <rect x="38" y="96" width="22" height="4" rx="2" fill="{AMBER_DARK}"/>'''


def chameleon(smile=True, tail=MINT):
    """Two-tone chameleon on the page's top edge; its curled tail ends in an arrow."""
    body = "M38 46 C37 34 47 26 59 26 C69 26 76 31 79 37 L79 46 Z"
    return f'''<path d="M39 41 C27 41 19 50 21 59 C23 67 33 69 36 62 C38 56 33 53 30 57" stroke="{tail}" stroke-width="4.6" stroke-linecap="round" fill="none"/>
  <path d="M25.6 52.6 L29.8 57.8 L35.6 56.2" stroke="{tail}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
  <path d="M48 45 L46 50.5 M44 51 L48 51.2" stroke="{INK}" stroke-width="3.2" stroke-linecap="round"/>
  <path d="{body}" fill="{MINT}"/>
  <path d="{body}" fill="{AMBER}" clip-path="url(#front)"/>
  <path d="M45 30.4 L47.6 27.6 L50.2 29.4 L52.8 26.6 L55.4 28.4 L58 25.9 L60.6 27.8 L63.2 25.9 L65.8 28.2 L68.4 27" stroke="{INK}" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none" stroke-opacity="0.75"/>
  <path d="M76 29.5 C84 27 93 30.5 96.5 37.5 C97.2 42.6 91 46.4 82 46.6 L77 46 Z" fill="{AMBER}"/>
  {'<path d="M87 43 Q91.6 43.8 95.2 41.4" stroke="'+AMBER_DARK+'" stroke-width="1.4" stroke-linecap="round" fill="none"/>' if smile else ''}
  <circle cx="83" cy="36.4" r="5.4" fill="#fff" stroke="{AMBER_DARK}" stroke-width="1.6"/>
  <circle cx="84.3" cy="36.6" r="2.4" fill="{PUPIL}"/>
  <path d="M70 45 L71.5 50.5 M68.5 51 L73 50.8" stroke="{INK}" stroke-width="3.2" stroke-linecap="round"/>'''


def icon():
    # macOS icon grid: 824px plate at a 100px inset, ~185px corners, soft shadow.
    s = 824 / 120
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  {defs()}
  <rect x="100" y="118" width="824" height="824" rx="185" fill="#000" fill-opacity="0.06"/>
  <rect x="100" y="110" width="824" height="824" rx="185" fill="#000" fill-opacity="0.08"/>
  <rect x="100" y="104" width="824" height="824" rx="185" fill="#000" fill-opacity="0.10"/>
  <g transform="translate(100 100) scale({s:.4f})">
  <rect width="120" height="120" rx="27" fill="url(#plate)"/>
  <rect width="120" height="120" rx="27" fill="url(#sheen)"/>
  <g transform="translate(0 -3)">{document()}
  {chameleon()}</g>
  </g>
</svg>
'''


def small_icon():
    """16–32px: page with converted half and a chunky two-tone chameleon, no fine detail."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  {defs()}
  <g transform="translate(64 64) scale({896/120:.4f})">
  <rect width="120" height="120" rx="27" fill="url(#plate)"/>
  <path d="M26 46 H78 L96 64 V108 H26 Z" fill="#fff"/>
  <path d="M26 82 H96 V108 H26 Z" fill="{AMBER}"/>
  <path d="M32 40 C18 40 12 52 18 60 C23 66 32 62 29 55" stroke="{MINT}" stroke-width="9" stroke-linecap="round" fill="none"/>
  <path d="M30 46 C30 30 44 20 60 20 C76 20 88 28 96 40 C88 46 76 47 60 46 Z" fill="{MINT}"/>
  <path d="M60 20 C76 20 88 28 96 40 C88 46 76 47 60 46 Z" fill="{AMBER}"/>
  <circle cx="78" cy="33" r="7" fill="#fff"/><circle cx="80" cy="33" r="3.2" fill="{PUPIL}"/>
  </g>
</svg>
'''


def mascot():
    """The character on its own (README, website, empty states): no plate."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="14 18 90 92" width="900" height="920">
  {defs()}
  {document(edge="#CFE6DC")}
  {chameleon(tail="#5DCAA5")}
</svg>
'''


for name, svg in {"chamo-icon.svg": icon(), "chamo-icon-small.svg": small_icon(), "chamo-mascot.svg": mascot()}.items():
    (OUT / name).write_text(svg)
    print("wrote", name)
