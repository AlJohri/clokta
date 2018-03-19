'''
Code-behind the scenes for the cli application.
'''
import json
import time

import boto3
import click
import requests
from bs4 import BeautifulSoup

from clokta.common import Common
from clokta.factor_chooser import FactorChooser
from clokta.profile_manager import ProfileManager


class RoleAssumer(object):
    ''' Core implementation of clokta '''

    def __init__(self, profile, verbose=False):
        ''' Constructor '''
        self.profile = profile
        self.verbose = verbose

    def assume_role(self):
        ''' entry point for the cli tool '''
        profile_mgr = ProfileManager(profile_name=self.profile, verbose=self.verbose)
        configuration = profile_mgr.initialize_configuration()
        profile_mgr.update_configuration(profile_configuration=configuration)

        session_token = self.__okta_session_token(
            configuration=configuration
        )
        if self.verbose:
            Common.dump_verbose(message='Okta session token: {}'.format(session_token))

        saml_assertion = self.__saml_assertion_aws(
            session_token=session_token,
            configuration=configuration
        )
        client = boto3.client('sts')
        assumed_role_credentials = client.assume_role_with_saml(
            RoleArn=configuration['okta_aws_role_to_assume'],
            PrincipalArn=configuration['okta_idp_provider'],
            SAMLAssertion=saml_assertion
        )

        profile_mgr.apply_credentials(credentials=assumed_role_credentials)
        profile_mgr.write_sourceable_file(credentials=assumed_role_credentials)
        profile_mgr.write_dockerenv_file(credentials=assumed_role_credentials)

    def __okta_session_token(self, configuration):
        ''' Authenticate with Okta; receive a session token '''
        okta_response = None

        try:
            okta_response = self.__okta_auth_response(configuration=configuration)
        except requests.exceptions.HTTPError as http_err:
            msg = 'Okta returned this credentials/password related error: {}'.format(http_err)
            Common.dump_err(message=msg, exit_code=1, verbose=self.verbose)
        except Exception as err:
            msg = 'Unexpected error: {}'.format(err)
            Common.dump_err(message=msg, exit_code=2, verbose=self.verbose)

        # handle case where MFA is required but no factors have been enabled
        if okta_response['status'] == 'MFA_ENROLL':
            msg = 'Please enroll in multi-factor authentication before using this tool'
            Common.dump_err(message=msg, exit_code=3, verbose=self.verbose)

        if okta_response['status'] == 'MFA_REQUIRED':
            factors = okta_response['_embedded']['factors']
            if factors:
                return self.__okta_session_token_mfa(
                    auth_response=okta_response,
                    factors=factors,
                    factor_preference=configuration['multifactor_preference']
                )
            else:
                msg = 'No MFA factors have been set up for this account'
                Common.dump_err(message=msg, exit_code=3, verbose=self.verbose)

        return okta_response['sessionToken']

    def __okta_session_token_mfa(self, auth_response, factors, factor_preference):
        ''' Determine which factor to use and apply it to get a session token '''
        factor = self.__choose_factor(
            factors=factors,
            factor_preference=factor_preference,
        )
        state_token = auth_response['stateToken']

        if factor['factorType'] == 'push':
            return self.__send_push(
                factor=factor,
                state_token=state_token
            )

        if factor['factorType'] == 'sms':
            self.__okta_mfa_verification(
                factor_dict=factor,
                state_token=state_token,
                otp_value=None
            )

        otp_value = click.prompt('Enter your multifactor authentication token', type=str)
        try:
            mfa_response = self.__okta_mfa_verification(
                factor_dict=factor,
                state_token=state_token,
                otp_value=otp_value
            )
            session_token = mfa_response['sessionToken']
        except requests.exceptions.HTTPError as http_err:
            msg = 'Okta returned this MFA related error: {}'.format(http_err)
            Common.dump_err(message=msg, exit_code=1, verbose=self.verbose)
        except Exception as err:
            msg = 'Unexpected error: {}'.format(err)
            Common.dump_err(message=msg, exit_code=2, verbose=self.verbose)

        return session_token

    def __send_push(self, factor, state_token):
        ''' Send push re: Okta Verify '''
        url = factor['_links']['verify']['href']
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cache-Control': '"no-cache'
        }
        payload = {
            'stateToken': state_token
        }

        response_data = None
        response = requests.post(url, data=json.dumps(payload), headers=headers)
        if response.status_code == requests.codes.ok:  # pylint: disable=E1101
            response_data = response.json()
        else:
            response.raise_for_status()

        Common.echo(message='Push notification sent; waiting for your response', new_line=False)

        status = response_data['status']
        if status == 'MFA_CHALLENGE':
            if 'factorResult' in response_data and response_data['factorResult'] == 'WAITING':
                return self.__check_push_result(
                    state_token=state_token,
                    push_response=response_data
                )


    def __check_push_result(self, state_token, push_response):
        ''' Wait for push response acknowledgement '''
        url = push_response['_links']['next']['href']
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cache-Control': '"no-cache'
        }
        payload = {
            'stateToken': state_token
        }

        wait_for = 60
        timeout = time.time() + wait_for
        response_data = None
        while True:
            Common.echo(message='.', new_line=False)
            response = requests.post(url, data=json.dumps(payload), headers=headers)
            if response.status_code == requests.codes.ok:  # pylint: disable=E1101
                response_data = response.json()
            else:
                response.raise_for_status()

            if 'sessionToken' in response_data or time.time() > timeout:
                Common.echo(message='Session confirmed')
                break
            time.sleep(3)

        if response_data:
            return response_data['sessionToken']
        else:
            msg = 'Timeout expired ({} seconds)'.format(wait_for)
            Common.dump_err(message=msg, exit_code=3)

    def __choose_factor(self, factors, factor_preference=None):
        ''' Automatically choose, or allow user to choose, the MFA option '''

        fact_chooser = FactorChooser(
            factors=factors,
            factor_preference=factor_preference,
            verbose=self.verbose
        )

        # Is there only one legitimate MFA option available?
        if len(factors) == 1:
            factor = fact_chooser.verify_only_factor(factor=factors[0])
            if factor:
                return factor

        # Has the user pre-selected a legitimate factor?
        if factor_preference:
            factor = fact_chooser.verify_preferred_factor()
            if factor:
                return factor

        return fact_chooser.choose_supported_factor()

    def __okta_mfa_verification(self, factor_dict, state_token, otp_value=None):
        ''' Sends the MFA token entered and retuns the response '''
        url = factor_dict['_links']['verify']['href']
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cache-Control': '"no-cache'
        }
        payload = {
            'stateToken': state_token
        }
        if otp_value:
            payload['answer'] = otp_value

        response = requests.post(url, data=json.dumps(payload), headers=headers)
        if response.status_code == requests.codes.ok:  # pylint: disable=E1101
            return response.json()
        else:
            response.raise_for_status()

    def __okta_auth_response(self, configuration):
        ''' Returns an HTTP response for credentials-based authentication with Okta '''
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cache-Control': '"no-cache',
            'Authorization': 'API_TOKEN'
        }
        payload = {
            'username': configuration['okta_username'],
            'password': configuration['okta_password']
        }
        url = 'https://' + configuration['okta_org'] + '/api/v1/authn'

        response = requests.post(url, data=json.dumps(payload), headers=headers)
        if response.status_code == requests.codes.ok:  # pylint: disable=E1101
            resp = json.loads(response.text)
            return resp
        else:
            response.raise_for_status()

    def __saml_assertion_aws(self, session_token, configuration):
        ''' fetch saml 2.0 assertion '''
        response = self.__okta_app_response(
            session_token=session_token,
            configuration=configuration
        )

        if self.verbose:
            Common.dump_verbose(
                message='SAML response: {}'.format(response.content)
            )

        soup = BeautifulSoup(response.content, "html.parser")
        assertion = None
        for inputtag in soup.find_all('input'):
            if inputtag.get('name') == 'SAMLResponse':
                assertion = inputtag.get('value')
        return assertion

    def __okta_app_response(self, session_token, configuration):
        url = configuration['okta_aws_app_url'] + '?onetimetoken=' + session_token
        response = requests.get(url)
        if response.status_code == requests.codes.ok:  # pylint: disable=E1101
            return response
        response.raise_for_status()