import { fireEvent, render } from '@testing-library/react-native';
import React from 'react';

import { AuthScreen } from '../../src/screens/AuthScreen';

describe('AuthScreen', () => {
  it('shows validation errors when present', () => {
    const { getByTestId, queryByTestId } = render(
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

  it('calls onSubmit when the sign in button is pressed', () => {
    const onSubmit = jest.fn();
    const { getByTestId } = render(
      <AuthScreen
        email="user@example.com"
        password="longenough"
        errors={{}}
        onEmailChange={jest.fn()}
        onPasswordChange={jest.fn()}
        onSubmit={onSubmit}
      />,
    );

    fireEvent.press(getByTestId('submit-button'));

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it('calls onEmailChange as the user types', () => {
    const onEmailChange = jest.fn();
    const { getByTestId } = render(
      <AuthScreen
        email=""
        password=""
        errors={{}}
        onEmailChange={onEmailChange}
        onPasswordChange={jest.fn()}
        onSubmit={jest.fn()}
      />,
    );

    fireEvent.changeText(getByTestId('email-input'), 'new@example.com');

    expect(onEmailChange).toHaveBeenCalledWith('new@example.com');
  });
});
