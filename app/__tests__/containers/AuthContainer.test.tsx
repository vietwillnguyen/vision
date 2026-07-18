import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import { AuthContainer } from '../../src/containers/AuthContainer';

describe('AuthContainer', () => {
  it('validates locally before calling signIn', () => {
    const signIn = jest.fn();
    render(<AuthContainer signIn={signIn} />);
    fireEvent.changeText(screen.getByTestId('email-input'), 'not-an-email');
    fireEvent.changeText(screen.getByTestId('password-input'), 'short');
    fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).not.toHaveBeenCalled();
    expect(screen.getByTestId('email-error')).toBeTruthy();
    expect(screen.getByTestId('password-error')).toBeTruthy();
  });

  it('calls signIn with valid credentials and surfaces auth errors', async () => {
    const signIn = jest.fn().mockResolvedValue('Invalid login credentials');
    render(<AuthContainer signIn={signIn} />);
    fireEvent.changeText(screen.getByTestId('email-input'), 'a@b.co');
    fireEvent.changeText(screen.getByTestId('password-input'), 'password123');
    fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).toHaveBeenCalledWith('a@b.co', 'password123');
    await waitFor(() =>
      expect(screen.getByTestId('auth-error')).toHaveTextContent('Invalid login credentials'),
    );
  });
});
