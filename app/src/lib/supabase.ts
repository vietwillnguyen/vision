import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL as string;
const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY as string;

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

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, supabaseClientOptions);
