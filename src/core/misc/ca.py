"""Certificate Authority generation utilities.

This module provides utilities for generating self-signed CA certificates
and server certificates with configurable parameters.
"""

from pathlib import Path
from typing import List

from core.logger import get_logger
from core.config import Application
from core.command import execute_command

logger = get_logger(__name__)


class CA:
    """Certificate Authority generator for self-signed certificates.

    This class generates CA certificates and server certificates with the following
    directory structure:
    /opt/kubengine/config/certs/
        ├── openssl.cnf    # OpenSSL configuration file
        ├── index          # Certificate index file
        ├── serial         # Certificate serial number file
        ├── ca/            # CA certificate directory
        │   ├── ca.crt     # CA root certificate (.crt format)
        │   └── ca.key     # CA private key
        └── server/        # Server certificate directory
            ├── server.crt # Server certificate
            ├── server.csr # Server CSR
            └── server.key # Server private key
    """

    # OpenSSL configuration template
    OPENSSL_CONF_TEMPLATE = """# Global default configuration
[default]
default_md = sha256        # Default hash algorithm
string_mask = utf8only     # UTF-8 characters only
prompt = no                # Non-interactive mode

# ==================== CSR/CA Certificate Core Configuration ====================
[req]
default_bits = 4096                # Key length (recommended 4096)
distinguished_name = req_distinguished_name  # Subject information section
x509_extensions = v3_ca            # Enable CA extensions for self-signed CA
req_extensions = v3_req            # Enable SAN extensions for CSR generation

# Certificate subject information (X.509 required fields)
[req_distinguished_name]
countryName = {country_code}                   # Country code (2 digits)
stateOrProvinceName = {state_name}      # Province/State
localityName = {locality_name}             # City
organizationName = {organization_name}       # Organization name
commonName = {ca_common_name}     # Your CA name
emailAddress = {email_address}    # Contact email (optional)

# ==================== CA Certificate Extensions (marked as root CA) ====================
[v3_ca]
basicConstraints = critical, CA:TRUE                         # Mark as CA (critical required)
keyUsage = critical, digitalSignature, cRLSign, keyCertSign  # CA key usage
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer:always

# ==================== CSR Extensions (used for server CSR generation) ====================
[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
subjectAltName = @alt_names        # SAN extension (HTTPS required)

# ==================== HTTPS Server Certificate Extensions (issued by CA) ====================
[server_cert]
basicConstraints = critical, CA:FALSE                   # Non-CA (critical required)
keyUsage = critical, digitalSignature, keyEncipherment  # Server key usage
extendedKeyUsage = serverAuth, clientAuth               # Support server/client authentication
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer:always
subjectAltName = @alt_names        # SAN (HTTPS mandatory)

# ==================== Auxiliary Configuration ====================
[alt_names]
# HTTPS domains/IPs (add as needed)
{san_entries}

# ==================== CA Issuing Policy Configuration ====================
[ca]
default_ca = CA_default     # Default CA configuration section

[CA_default]
dir = {base_dir}                     # CA working directory
certificate = $dir/ca/ca.crt         # CA certificate path
private_key = $dir/ca/ca.key         # CA private key path
new_certs_dir = $dir                 # Directory for issued certificates
database = $dir/index                # Certificate database (record issued certificates)
serial = $dir/serial                 # Certificate serial number (initial value)
default_days = {ca_valid_days}                  # CA certificate validity period
default_crl_days = 30                # CRL validity period (30 days)
default_md = sha256                  # Hash algorithm
preserve = no                        # Preserve request files
policy = policy_match                # Issuing policy

# Issuing policy: Server certificates must match some CA fields
[policy_match]
countryName             = match     # Must match CA
stateOrProvinceName     = match
organizationName        = match
organizationalUnitName  = optional
commonName              = supplied  # Server domain provided by CSR
emailAddress            = optional
"""

    def __init__(self) -> None:
        """Initialize CA certificate generator."""
        self._validate_application_config()
        self._initialize_paths()
        self._initialize_directories()
        self._initialize_config()

    def _validate_application_config(self) -> None:
        """Validate that required Application properties are configured."""
        required_props: dict[str, str | int] = {
            'DOMAIN': Application.DOMAIN,
            'TLS_ROOT_DIR': Application.TLS_CONFIG.ROOT_DIR,
            'CA_COUNTRY_CODE': Application.TLS_CONFIG.CA_COUNTRY_CODE,
            'CA_STATE_NAME': Application.TLS_CONFIG.CA_STATE_NAME,
            'CA_LOCALITY_NAME': Application.TLS_CONFIG.CA_LOCALITY_NAME,
            'CA_ORGANIZATION_NAME': Application.TLS_CONFIG.CA_ORGANIZATION_NAME,
            'CA_COMMON_NAME': Application.TLS_CONFIG.CA_COMMON_NAME,
            'CA_EMAIL_ADDRESS': Application.TLS_CONFIG.CA_EMAIL_ADDRESS,
            'CA_PASSWORD': Application.TLS_CONFIG.CA_PASSWORD,
            'CA_VALID_DAYS': Application.TLS_CONFIG.CA_VALID_DAYS,
            'CA_KEY_LENGTH': Application.TLS_CONFIG.CA_KEY_LENGTH
        }

        missing_props = [prop for prop,
                         value in required_props.items() if not value]

        if missing_props:
            raise ValueError(
                f"Missing Application configuration: {', '.join(missing_props)}")

    def _initialize_paths(self) -> None:
        """Initialize file paths."""
        self.server_san: List[str] = [
            Application.DOMAIN, f'*.{Application.DOMAIN}']
        self.base_dir = Path(Application.TLS_CONFIG.ROOT_DIR)

        # CA related paths
        self.ca_dir = self.base_dir / "ca"
        self.ca_key_path = self.ca_dir / "ca.key"
        self.ca_cert_path = self.ca_dir / "ca.crt"

        # Server related paths
        self.server_dir = self.base_dir / "server"
        self.server_cert_path = self.server_dir / "server.crt"
        self.server_csr_path = self.server_dir / "server.csr"
        self.server_key_path = self.server_dir / "server.key"

        # Index and serial files
        self.index_path = self.base_dir / "index"
        self.serial_path = self.base_dir / "serial"

    def _initialize_directories(self) -> None:
        """Create and clean directories."""
        self._clean_existing_files()
        self.ca_dir.mkdir(parents=True, exist_ok=True)
        self.server_dir.mkdir(parents=True, exist_ok=True)

    def _initialize_config(self) -> None:
        """Initialize configuration values."""
        self.ca_valid_days = Application.TLS_CONFIG.CA_VALID_DAYS
        self.ca_password = Application.TLS_CONFIG.CA_PASSWORD
        self.ca_key_length = Application.TLS_CONFIG.CA_KEY_LENGTH

        # Initialize index and serial files
        self.index_path.touch(exist_ok=True)
        self.serial_path.write_text("1000")

    def _clean_existing_files(self) -> None:
        """Clean existing files in base directory."""
        if self.base_dir.exists():
            for item in self.base_dir.rglob("*"):
                if item.is_file():
                    item.unlink()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def generate_ca(self) -> bool:
        """Generate CA certificate and server certificate.

        Returns:
            True if successful, False otherwise.
        """
        try:
            logger.debug("Starting certificate chain generation...")
            self._generate_openssl_config()
            self._generate_ca_cert()
            self._generate_server_cert()
            self._verify_certs()
            self._cleanup_extra_files()

            logger.info("Certificate generation completed! File paths:")
            logger.info(f"CA certificate: {self.ca_cert_path}")
            logger.info(f"Server certificate: {self.server_cert_path}")
            return True
        except Exception as e:
            logger.error(f"Generation failed: {str(e)}")
            return False
        except SystemExit:
            logger.error("Certificate generation interrupted")
            return False

    def _generate_openssl_config(self) -> None:
        """Generate OpenSSL configuration file."""
        logger.debug("Generating OpenSSL configuration file...")
        san_entries = "\n".join(
            f"DNS.{i}   = {dns}" for i, dns in enumerate(self.server_san, 1))

        # Fill template with Application configuration
        config_content = self.OPENSSL_CONF_TEMPLATE.format(
            base_dir=str(self.base_dir),
            san_entries=san_entries,
            ca_valid_days=self.ca_valid_days,
            country_code=Application.TLS_CONFIG.CA_COUNTRY_CODE,
            state_name=Application.TLS_CONFIG.CA_STATE_NAME,
            locality_name=Application.TLS_CONFIG.CA_LOCALITY_NAME,
            organization_name=Application.TLS_CONFIG.CA_ORGANIZATION_NAME,
            ca_common_name=Application.TLS_CONFIG.CA_COMMON_NAME,
            email_address=Application.TLS_CONFIG.CA_EMAIL_ADDRESS
        )

        self.openssl_cnf = self.base_dir / "openssl.cnf"
        self.openssl_cnf.write_text(config_content, encoding="utf-8")
        logger.debug("OpenSSL configuration file generated")

    def _generate_ca_cert(self) -> None:
        """Generate CA certificate."""
        # Generate CA private key
        logger.debug("Generating CA private key...")
        genrsa_cmd = ["openssl", "genrsa",
                      "-out", str(self.ca_key_path), str(self.ca_key_length)]
        execute_command(" ".join(genrsa_cmd)).exit_if_failed()
        self.ca_key_path.chmod(0o400)

        # Generate CA self-signed certificate
        logger.debug("Generating CA self-signed certificate...")
        req_cmd = [
            "openssl", "req", "-config", str(self.openssl_cnf),
            "-key", str(self.ca_key_path),
            "-new", "-x509", "-days", str(self.ca_valid_days),
            "-sha256", "-extensions", "v3_ca",
            "-out", str(self.ca_cert_path)
        ]
        execute_command(" ".join(req_cmd)).exit_if_failed()
        self.ca_cert_path.chmod(0o644)

    def _generate_server_cert(self) -> None:
        """Generate server certificate."""
        # Generate server private key
        genrsa_cmd = ["openssl", "genrsa", "-out",
                      str(self.server_key_path), str(self.ca_key_length)]
        execute_command(" ".join(genrsa_cmd)).exit_if_failed()
        self.server_key_path.chmod(0o400)

        # Generate server CSR
        req_cmd = [
            "openssl", "req", "-config", str(self.openssl_cnf),
            "-key", str(self.server_key_path),
            "-new", "-sha256", "-out", str(self.server_csr_path)
        ]
        execute_command(" ".join(req_cmd)).exit_if_failed()

        # CA signs server certificate
        ca_cmd = [
            "openssl", "ca", "-config", str(self.openssl_cnf),
            "-extensions", "server_cert", "-days", str(self.ca_valid_days),
            "-in", str(self.server_csr_path), "-out", str(self.server_cert_path),
            "-batch"
        ]
        execute_command(" ".join(ca_cmd)).exit_if_failed()
        self.server_cert_path.chmod(0o400)

    def _verify_certs(self) -> None:
        """Verify generated certificates."""
        logger.debug("Verifying certificates...")
        # Verify CA certificate
        verify_cmd = ["openssl", "x509", "-in",
                      str(self.ca_cert_path), "-noout", "-text"]
        execute_command(" ".join(verify_cmd)).exit_if_failed()

        # Verify server certificate
        verify_cmd = ["openssl", "x509", "-in",
                      str(self.server_cert_path), "-noout", "-text"]
        execute_command(" ".join(verify_cmd)).exit_if_failed()

    def _cleanup_extra_files(self) -> None:
        """Clean up extra files, keep only necessary structure."""
        keep_files = {
            self.openssl_cnf,
            self.index_path,
            self.serial_path,
            self.ca_cert_path,
            self.ca_key_path,
            self.server_cert_path,
            self.server_csr_path,
            self.server_key_path
        }

        for item in self.base_dir.rglob("*"):
            if item.is_file() and item not in keep_files:
                try:
                    item.unlink()
                    logger.debug(f"Deleted extra file: {item}")
                except Exception as e:
                    logger.warning(f"Failed to delete file {item}: {e}")


def create_cert() -> None:
    """Create certificates."""
    # Check if .Done file exists in ROOT_DIR
    tls_root_dir = Application.TLS_CONFIG.ROOT_DIR
    if not tls_root_dir:
        raise ValueError("Application.TLS_ROOT_DIR is not configured")

    done_file = Path(tls_root_dir) / ".Done"
    if done_file.exists():
        return

    ca = CA()
    result = ca.generate_ca()
    if not result:
        raise RuntimeError("Failed to create CA certificate.")
    done_file.touch(exist_ok=True)


def k8s_create_tls(namespace: str, tls_name: str) -> None:
    """Create TLS certificate in specified Kubernetes namespace.

    Args:
        namespace: Kubernetes namespace name
        tls_name: TLS certificate name

    Raises:
        ValueError: When Application configuration is incomplete
        RuntimeError: When TLS certificate creation fails
    """
    # Check Application configuration
    if not Application.TLS_CONFIG.SERVER_CRT or not Application.TLS_CONFIG.SERVER_KEY:
        raise ValueError(
            "Application SERVER_CRT or SERVER_KEY is not configured")

    execute_command(
        f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl create namespace {namespace}")

    res = execute_command(
        f"KUBECONFIG=/etc/kubernetes/admin.conf kubectl create secret tls {tls_name} "
        f"--cert={Application.TLS_CONFIG.SERVER_CRT} --key={Application.TLS_CONFIG.SERVER_KEY} -n {namespace}"
    )

    if res.is_failure():
        raise RuntimeError(
            f"Failed to create TLS cert. {res.get_error_lines()}")
