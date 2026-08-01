import AsyncStorage from '@react-native-async-storage/async-storage';
import { supabaseClientOptions } from '../../src/lib/supabase';

jest.mock('@supabase/supabase-js', () => ({
  createClient: jest.fn(() => ({})),
}));

jest.mock('@react-native-async-storage/async-storage', () => ({
  setItem: jest.fn(() => Promise.resolve()),
  getItem: jest.fn(() => Promise.resolve(null)),
  removeItem: jest.fn(() => Promise.resolve()),
  multiSet: jest.fn(() => Promise.resolve()),
  multiGet: jest.fn(() => Promise.resolve([])),
  getAllKeys: jest.fn(() => Promise.resolve([])),
  clear: jest.fn(() => Promise.resolve()),
}));

process.env.EXPO_PUBLIC_SUPABASE_URL = 'https://placeholder.supabase.co';
process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY = 'placeholder';

describe('supabaseClientOptions', () => {
  it('persists sessions to AsyncStorage', () => {
    expect(supabaseClientOptions.auth).toEqual({
      storage: AsyncStorage,
      persistSession: true,
      detectSessionInUrl: false,
      autoRefreshToken: true,
    });
  });
});
