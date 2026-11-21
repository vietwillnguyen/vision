/**
 * The public demo's sign-in, read from public build-time config.
 *
 * `EXPO_PUBLIC_DEMO_EMAIL` and `EXPO_PUBLIC_DEMO_PASSWORD` are inlined into the
 * bundle at build time, so anyone can read them - which is fine, because the
 * account they name is read-only in the database (see
 * `supabase/migrations/20261121120000_read_only_accounts.sql`). When either is
 * unset the "View demo" button is not shown at all.
 */
export interface DemoConfig {
  email: string;
  password: string;
}

export function parseDemoConfig(
  email: string | undefined,
  password: string | undefined,
): DemoConfig | null {
  const trimmedEmail = email?.trim();
  if (!trimmedEmail || !password) {
    return null;
  }
  return { email: trimmedEmail, password };
}

// Expo only inlines EXPO_PUBLIC_* variables read as static `process.env.NAME`
// member expressions, so these two reads are the one place the demo is wired.
export const demoConfig: DemoConfig | null = parseDemoConfig(
  process.env.EXPO_PUBLIC_DEMO_EMAIL,
  process.env.EXPO_PUBLIC_DEMO_PASSWORD,
);
