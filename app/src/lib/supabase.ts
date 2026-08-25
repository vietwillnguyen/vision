import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';

import type { Database } from '../generated/database';

const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL as string;
const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY as string;

/**
 * The app's Supabase client, parameterised by the generated schema.
 *
 * Everything that queries the database takes this type rather than a bare
 * `SupabaseClient`, so `.from('reels').select('*')` resolves to the real column
 * list - names and nullability included - and `tsc` rejects a query the schema
 * cannot answer. Regenerate `src/generated/database.ts` with `npm run gen:types`
 * after a migration.
 */
export type VisionClient = SupabaseClient<Database>;

// React Native has no URL-based session detection and needs explicit storage,
// or sessions will not survive app restarts.
export const supabaseClientOptions = {
  auth: {
    storage: AsyncStorage,
    persistSession: true,
    detectSessionInUrl: false,
    autoRefreshToken: true,
  },
};

export const supabase: VisionClient = createClient<Database>(
  SUPABASE_URL,
  SUPABASE_ANON_KEY,
  supabaseClientOptions,
);
