import type {Plugin} from 'vite';

export function licenseOf(manifest: Record<string, unknown>): string;
export function bundleLicenseError(
  name: string,
  expression: string,
): string | undefined;
export function toolLicenseError(
  name: string,
  expression: string,
): string | undefined;
export function installedLicenseErrors(nodeModulesDirs: string[]): string[];
export function packageRootOf(id: string): string | undefined;
export function renderNotices(packageRoots: Iterable<string>): {
  text: string;
  errors: string[];
};
export function thirdPartyLicenses(options: {root: string}): Plugin;
