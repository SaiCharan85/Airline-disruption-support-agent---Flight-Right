# Assets

Where every image, icon and animation in the console (`ui/`) comes from. No scraped assets.

## Self-made (no third-party rights)

| Asset | File | Notes |
|---|---|---|
| Logo (paper plane with a check) | `ui/public/logo.svg` | Hand-written SVG; also the favicon |
| Charts (stacked bars, rate bars, trend line with crosshair) | `ui/src/components/charts.tsx` | Plain SVG/CSS. Colors are the categorical slots 1-3 of a palette checked for colour-vision deficiency in light and dark mode; every value is also shown as text |
| Motion (pipeline steps and live-feed rows rising in, drawer sliding in) | `ui/src/styles.css` | CSS keyframes, 200-260 ms; switched off under `prefers-reduced-motion` |

## Icons: Lucide

`lucide-react` 1.64.0 from npm (ISC license, Lucide Icons and Contributors; some icons derive from Feather, MIT).
The license text is in `ui/licenses/lucide-LICENSE.txt`. Icons are imported individually, so only the ones used ship.

## Fonts

None bundled: the console uses the system UI font stack.
