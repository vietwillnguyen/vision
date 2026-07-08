export interface AuthFormErrors {
  email?: string;
  password?: string;
}

export function isValidEmail(email: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

export function isValidPassword(password: string): boolean {
  return password.length >= 8;
}

export function validateAuthForm(email: string, password: string): AuthFormErrors {
  const errors: AuthFormErrors = {};
  if (!isValidEmail(email)) {
    errors.email = 'Enter a valid email address';
  }
  if (!isValidPassword(password)) {
    errors.password = 'Password must be at least 8 characters';
  }
  return errors;
}
