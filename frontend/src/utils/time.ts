/**
 * utils/time.ts – közös időkezelő segédfüggvények
 */

/**
 * A backend created_at-je UTC datetime, ami ISO stringként érkezik,
 * tipikusan időzóna-jelző nélkül (datetime.utcnow()). Ha nincs 'Z' vagy
 * offset a végén, kézzel UTC-ként értelmezzük, különben a böngésző
 * helyi időként parse-olná, és az idő eltolódna az időzóna-különbséggel.
 *
 * Visszatér: epoch ezredmásodpercben.
 */
export function parseUtc(iso: string): number {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)
  return new Date(hasTz ? iso : `${iso}Z`).getTime()
}
