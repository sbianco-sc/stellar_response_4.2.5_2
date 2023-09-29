import json
import uuid
import requests
import jwt

import utils
from datetime import datetime, timedelta

VALID_ACTIONS = ["contain_host"]

class CylanceConnector:

    def __init__(self, service_endpoint_url, tenant_id, app_id, app_secret, lgr=None, **kwargs):
        """
        Object that creates the authorization headers and sends API requests to the Cylance APIs'.
        :param service_endpoint_url: URL of Cylance service endpoint
        :param tenant_id: ID of the tenant in Cylance
        :param app_id: ID of the application created in Cylance
        :param app_secret: key (secret) generated by the application created in Cylance
        """
        self.base_url = service_endpoint_url.rstrip('/')
        self.tenant_id = tenant_id
        self.app_id = app_id
        self.app_secret = utils.aella_decode(utils.COLLECTOR_SECRET, app_secret)
        self.logger = lgr
        self._headers = None
        requests.packages.urllib3.disable_warnings()

    @property
    def headers(self):
        """
        Generate headers
        :return: authorization headers to use in https requests
        """
        self._headers = self.login()
        return self._headers

    def login(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        auth_url = utils.build_url(self.base_url, "/auth/v2/token", logger=self.logger)

        # token will be valid for 30 minutes from now
        timeout = 1800
        now = datetime.utcnow()
        timeout_datetime = now + timedelta(seconds=timeout)
        epoch_time = int((now - datetime(1970, 1, 1)).total_seconds())
        epoch_timeout = int((timeout_datetime - datetime(1970, 1, 1)).total_seconds())
        claims = {
        "exp": epoch_timeout,
        "iat": epoch_time,
        "iss": "http://cylance.com",
        "sub": self.app_id,
        "tid": self.tenant_id,
        "jti": str(uuid.uuid4())
        }
        encoded = jwt.encode(claims, self.app_secret, algorithm='HS256').decode('utf-8')
        payload = {"auth_token": encoded}
        r = requests.post(auth_url, headers=headers, data=json.dumps(payload), verify=False)
        resp = r.json()
        try:
            assert r.status_code / 100 == 2
            headers['authorization'] = 'bearer ' + resp['access_token']
            headers['Content-Type'] = 'application/json'
            headers['accept'] = 'application/json'
            return headers
        except Exception as e:
            error_msg = "Failed to log in to Cylance: {}".format(str(e))
            if self.logger:
                self.logger.error(error_msg)
            raise Exception(error_msg)

    def prepare(self, action_name, settings, source):
        """
        Function used by Stellar Cyber for threat hunting integration
        :param action_name: str: function to call
        :param settings: obj: additional info that may be needed
        :param source: obj: threat hunting results
        :return: list of obj: list of parameters to call <action_name> with.
            Each object in the list represents one function call, so a list
            of length n will result in n separate calls
        """
        params = []
        # No implementation for now since this connector does not support automation
        return params

    def test_connection(self, **kwargs):
        try:
            if self.headers:
                return utils.create_response("Cylance", 200, "")
        except Exception as e:
            return utils.create_response("Cylance", 400, str(e))

    def contain_host(self, hostname=None, device_id=None, mac=None, duration=None, **kwargs):
        return self.run_cylance_action(
                "contain_host", hostname=hostname, device_id=device_id, mac=mac, duration=duration)

    def run_cylance_action(self, action, hostname=None, device_id=None, mac=None, duration="5 Minutes"):
        if hostname is None and device_id is None and mac is None:
            raise Exception("Missing target host info")
        if not device_id:
            if hostname:
                device_id = self.extract_device_from_action_reply(self.get_device_by_hostname(hostname))
            elif mac:
                device_id = self.extract_device_from_action_reply(self.get_device_by_mac(mac))
        reply = self.call_cylance_action(device_id=device_id, action=action, duration=duration)
        return self.extract_message_from_action_reply(reply)

    def get_device_by_hostname(self, hostname):
        url = utils.build_url(self.base_url, "/devices/v2/hostname/{}".format(hostname), logger=self.logger)
        response = self.call_cylance_api(url, "get", raw_url=url)
        return response

    def get_device_by_mac(self, mac):
        url = utils.build_url(self.base_url, "/devices/v2/macaddress/{}".format(mac), logger=self.logger)
        response = self.call_cylance_api(url, "get", raw_url=url)
        return response

    def extract_device_from_action_reply(self, reply):
        if reply.status_code/100 == 2:
            device_list = reply.json()  
            device_id = None
            for device in device_list:
                device_id = device.get("id","")
                if device_id:
                    break
            if not device_id:
                raise Exception("Device not found")
            return device_id
        raise Exception("Device not found")

    def extract_message_from_action_reply(self, reply):
        if reply.status_code/100 == 2:
            return {"result_msg": "Action succeeded"}
        if reply.status_code == 404:
            raise Exception("Failed to take action. Device not found.")
        try:
            errors = reply.json().get("message")
        except Exception as e:
            raise Exception("Invalid response received from Cylance server")
        raise Exception(json.dumps(errors))

    def call_cylance_api(self, url, method, raw_url=False, params={}):
        """
        Make an API requests.
        :param url: string
        :param method: bool
        :return: response
        """
        if not raw_url:
            url = utils.build_url(self.base_url, "/devices/v2", logger=self.logger)
        if method == "get":
            status = requests.get(url, headers=self.headers, verify=False, params=params, timeout=120)
        elif method == "put":
            status = requests.put(url, headers=self.headers, verify=False, params=params, timeout=120)
        return status

    def call_cylance_action(self, device_id="", action=None, duration="5 Minutes"):
        """
        action are for the following actions: 
            contain_host - Create a CylanceOPTICS Device Lockdown command resource for a specific device.
        :return: response
        """
        if action == "contain_host":
            device_id = device_id.replace("-", "").upper()
            url = utils.build_url(self.base_url, "/devicecommands/v2/{}/lockdown".format(device_id), logger=self.logger)
            params = {"value": "true"}
            fields = duration.split()
            num = int(fields[0])
            unit = fields[1].lower()
            if unit == "days":
                params["expires"] = "{0}:00:00".format(num)
            elif unit == "hours":
                day_string = int(num / 24)
                hour_string = self.expires_param_helpler(num % 24)
                params["expires"] = "{0}:{1}:00".format(day_string, hour_string)
            elif unit == "minutes":
                day_string = int(num / 1440)
                hour_string = self.expires_param_helpler(int((num % 1440) / 60))
                minute_string = self.expires_param_helpler(num % 1440 % 60)
                params["expires"] = "{0}:{1}:{2}".format(day_string, hour_string, minute_string)
            else:
                print("illegal action")
                return
        else:
            print("illegal action")
            return
        if self.logger:
                self.logger.info("Sending request {0} to Cylance with params{1}".format(url, params))
        response = self.call_cylance_api(url, "put", raw_url=url, params=params)
        return response
    
    def expires_param_helpler(self, num):
        if num < 10:
            return "0{}".format(num)
        else:
            return str(num)
        