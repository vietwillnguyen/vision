import { validateAuthForm } from '../../src/logic/authValidation';

describe('validateAuthForm', () => {
  it('returns no errors for a valid email and password', () => {
    expect(validateAuthForm('user@example.com', 'longenough')).toEqual({});
  });

  it('flags an invalid email', () => {
    expect(validateAuthForm('not-an-email', 'longenough')).toEqual({
      email: 'Enter a valid email address',
    });
  });

  it('flags a password shorter than 8 characters', () => {
    expect(validateAuthForm('user@example.com', 'short')).toEqual({
      password: 'Password must be at least 8 characters',
    });
  });
});
