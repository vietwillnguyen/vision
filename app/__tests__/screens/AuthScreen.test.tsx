import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

import { AuthScreen } from '../../src/screens/AuthScreen';

describe('AuthScreen', () => {
  it('shows validation errors when present', async () => {
    const { getByTestId, queryByTestId } = await render(
      <AuthScreen
        email="bad-email"
        password=""
        errors={{ email: 'Enter a valid email address' }}
        onEmailChange={jest.fn()}
        onPasswordChange={jest.fn()}
        onSubmit={jest.fn()}
      />,
    );

    expect(getByTestId('email-error').props.children).toBe('Enter a valid email address');
    expect(queryByTestId('password-error')).toBeNull();
  });

  it('calls onSubmit when the sign in button is pressed', async () => {
    const onSubmit = jest.fn();
    const { getByTestId } = await render(
      <AuthScreen
        email="user@example.com"
        password="longenough"
        errors={{}}
        onEmailChange={jest.fn()}
        onPasswordChange={jest.fn()}
        onSubmit={onSubmit}
      />,
    );

    await fireEvent.press(getByTestId('submit-button'));

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('calls onEmailChange as the user types', async () => {
    const onEmailChange = jest.fn();
    const { getByTestId } = await render(
      <AuthScreen
        email=""
        password=""
        errors={{}}
        onEmailChange={onEmailChange}
        onPasswordChange={jest.fn()}
        onSubmit={jest.fn()}
      />,
    );

    await fireEvent.changeText(getByTestId('email-input'), 'new@example.com');

    expect(onEmailChange).toHaveBeenCalledWith('new@example.com');
  });

  it('renders an auth error when provided', async () => {
    await render(
      <AuthScreen
        email=""
        password=""
        errors={{}}
        authError="Invalid login credentials"
        onEmailChange={jest.fn()}
        onPasswordChange={jest.fn()}
        onSubmit={jest.fn()}
      />,
    );
    expect(screen.getByTestId('auth-error')).toHaveTextContent('Invalid login credentials');
  });

  it('labels inputs for accessibility', async () => {
    await render(
      <AuthScreen
        email=""
        password=""
        errors={{}}
        onEmailChange={jest.fn()}
        onPasswordChange={jest.fn()}
        onSubmit={jest.fn()}
      />,
    );
    expect(screen.getByLabelText('Email address')).toBeTruthy();
    expect(screen.getByLabelText('Password')).toBeTruthy();
    expect(screen.getByLabelText('Sign in')).toBeTruthy();
  });
});
