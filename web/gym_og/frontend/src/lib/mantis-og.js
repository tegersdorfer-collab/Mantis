// mantis: This build switch keeps the upstream openGym app focused on the Mantis-backed read-only view.
export const MANTIS_OG = import.meta.env?.VITE_MANTIS_OG === '1'
