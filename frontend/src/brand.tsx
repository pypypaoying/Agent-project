/** Product identity; legacy package and storage identifiers stay compatible. */
export const BRAND_NAME = 'Analyvia';
export function BrandMark({ className = '' }: { className?: string }) {
  return <img className={`brand-mark ${className}`} src="/analyvia.svg" alt="" aria-hidden="true" />;
}
