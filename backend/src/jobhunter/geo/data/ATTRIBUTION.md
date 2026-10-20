# Attribution

Contains data from GeoNames (geonames.org), CC BY 4.0
(<https://creativecommons.org/licenses/by/4.0/>).

`de_places.tsv` and `non_de_places.tsv` are generated from the GeoNames files below by
`scripts/build_gazetteer.py`. The data was changed: it was filtered, names were folded (accents removed,
case-folded) and extended with umlaut transliterations, postal codes were attached to places, and the main
name of Munich and Nuremberg was replaced by the German one. `country_names_de.tsv` is hand-maintained and not
GeoNames data.

## Provenance

Downloaded on 2026-10-18 from <https://download.geonames.org/export/>.

| File | Last-Modified | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `dump/DE.zip` | Sun, 18 Oct 2026 02:35:29 GMT | 7,496,871 | `ea5aa79722b47f05491840e73ad2243e1558016e03e8dfd04c09df132b56a256` |
| `zip/DE.zip` | Sun, 18 Oct 2026 02:48:29 GMT | 383,939 | `57bee1598d73ebf81ea2732cc8bd842cbf7cbe735d63acc851798ad85ed28510` |
| `dump/cities15000.zip` | Sun, 18 Oct 2026 02:41:39 GMT | 3,361,901 | `7d1c80d5be04850227384c1f6f9163b56401dc46270be1012e1a2de0a5bf4a70` |
| `dump/countryInfo.txt` | Sun, 18 Oct 2026 02:48:11 GMT | 31,690 | `c55f638cfaafb4fe45a9aa15120a0f1f78e8a34a8e1847d7b9320cf740fbc9ab` |

## Filters

- `de_places.tsv`: feature class `P` in country `DE` with a population of at least 1,000,
  with its municipality (admin4) code. Alternate names: Latin script, capitalised, no short all-capitals
  code. A postal code maps to the places with the same folded name in the same state, else to the nearest
  place within 10 km.
- `non_de_places.tsv`: cities of `cities15000` with more than 100,000 inhabitants in a
  country of continent `EU` other than `DE`, under their name and usable alternate names; every country name
  other than Germany in English (`countryInfo.txt`) and German (`country_names_de.tsv`).

## Rows

- `de_places.tsv`: 7,662 places, 10,794 distinct postal codes
- `non_de_places.tsv`: 8,741 city rows (871 cities),
  423 country rows
