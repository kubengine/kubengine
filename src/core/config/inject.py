"""Configuration injection utilities.

This module provides decorators for injecting configuration values into classes
and methods, with support for nested class injection and default initialization.
"""

from typing import Any, Callable, TypeVar, Union, cast, get_type_hints
from functools import wraps
from .config_dict import ConfigDict


T = TypeVar('T', bound=type)
F = TypeVar('F', bound=Callable[..., Any])


def inject_config(prefix: str | None = None) -> Callable[[Union[T, F]], Union[T, F]]:
    """Configuration injection decorator: inject global configuration into classes/methods.

    Args:
        prefix: Configuration prefix for distinguishing different module configurations
                (e.g., "kubernetes" corresponds to config.kubernetes)

    Returns:
        Decorated class or function
    """
    def decorator(obj: Union[T, F]) -> Union[T, F]:
        # If decorating a class, inject into __init__ method
        if isinstance(obj, type):
            class WrappedClass(obj):  # type: ignore[misc]
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    # Inject configuration into instance attributes
                    config = ConfigDict.get_instance()
                    if prefix:
                        config = getattr(config, prefix)
                    self.config = config
                    super().__init__(*args, **kwargs)  # type: ignore

            return cast(Union[T, F], WrappedClass)

        # If decorating a function, inject into function parameters
        @wraps(obj)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            config = ConfigDict.get_instance()
            if prefix:
                config = getattr(config, prefix)
                kwargs['config'] = config
            return obj(*args, **kwargs)

        return cast(Union[T, F], wrapper)

    return decorator


def map_config_to_class(**config_mapping: str) -> Callable[[T], T]:
    """Class decorator: map global configuration to class attributes with nested class support.

    This decorator supports not only simple value injection but also injection into
    nested class attributes when the attribute type is a class with its own
    configuration mapping. It also creates default instances when configuration is missing.

    Args:
        **config_mapping: Configuration item mapping, format {'attr_name': 'config_path'}

    Returns:
        Decorated class with injected configuration values

    Example:
        class AuthConfig:
            ENABLED: bool
            SECRET_KEY: str

        @map_config_to_class(
            DOMAIN="domain",
            AUTH="auth"  # If AUTH has type AuthConfig, it will inject config into AuthConfig
        )
        class Application:
            DOMAIN: str
            AUTH: AuthConfig
    """
    def decorator(cls: T) -> T:
        config = ConfigDict.get_instance()

        # Get type hints for the class
        type_hints = get_type_hints(cls)

        for attr_name, config_path in config_mapping.items():
            # Parse configuration path (e.g., server.hosts -> config.server.hosts)
            config_value = None
            try:
                config_value = config
                for part in config_path.split("."):
                    config_value = getattr(config_value, part)
            except (AttributeError, KeyError):
                # Configuration path doesn't exist, config_value remains None
                config_value = None

            # Check if the attribute has a type hint and if it's a class
            if attr_name in type_hints:
                attr_type = type_hints[attr_name]

                # If the attribute type is a class, always create an instance
                if isinstance(attr_type, type) and attr_type is not type(None):
                    if config_value is not None:
                        # Configuration exists, create instance and inject
                        instance = _create_instance_with_config(
                            attr_type, config, config_path)
                    else:
                        # No configuration, create default instance
                        instance = _create_default_instance(attr_type)

                    # Set the instance as class attribute
                    setattr(cls, attr_name, instance)
                else:
                    # Direct assignment for non-class types
                    setattr(cls, attr_name, config_value)
            else:
                # No type hint, direct assignment
                setattr(cls, attr_name, config_value)

        return cls

    return decorator


def _create_instance_with_config(cls_type: type, config: Any, config_path: str) -> Any:
    """Create an instance of a class and inject configuration into it.

    Args:
        cls_type: The class type to instantiate
        config: The configuration object to inject from
        config_path: The configuration path for this instance

    Returns:
        Instance with injected configuration
    """
    instance = cls_type()
    # Always set default values first for all class attributes
    # This ensures that even unmapped attributes have their class defaults
    for attr_name in dir(cls_type):
        if not attr_name.startswith('_') and hasattr(cls_type, attr_name):
            # Skip properties, only set regular class attributes
            attr = getattr(cls_type, attr_name)
            if not isinstance(attr, property):
                attr_value = getattr(cls_type, attr_name)
                setattr(instance, attr_name, attr_value)

    if hasattr(cls_type, '_config_mapping'):
        config_mapping = getattr(cls_type, '_config_mapping')

        for attr_name, attr_config_path in config_mapping.items():
            try:
                path_parts = config_path.split(".")[:-1]
                full_path = ".".join(
                    path_parts + [attr_config_path]) if path_parts else attr_config_path
                value = config
                for part in full_path.split("."):
                    value = getattr(value, part)

                # Set the configuration value (overriding default)
                # Skip if it's a property
                if not hasattr(cls_type, attr_name) or not isinstance(getattr(cls_type, attr_name), property) and value is not None:
                    setattr(instance, attr_name, value)

            except (AttributeError, KeyError):
                # Configuration path doesn't exist, keep the default value
                # (already set from class attributes above)
                pass
    else:
        # If the class doesn't have config mapping, try to inject from config dict directly
        try:
            config_obj = config
            for part in config_path.split("."):
                config_obj = getattr(config_obj, part)

            # If config_obj is dict-like, set all matching attributes (skip properties)
            if hasattr(config_obj, '__dict__'):
                for key, val in config_obj.__dict__.items():
                    if not key.startswith('_') and hasattr(instance, key):
                        if not hasattr(cls_type, key) or not isinstance(getattr(cls_type, key), property):
                            setattr(instance, key, val)
        except (AttributeError, KeyError):
            # If path doesn't exist, just use the default instance
            pass

    return instance


def _create_default_instance(cls_type: type) -> Any:
    """Create a default instance of a class with its default values.

    Args:
        cls_type: The class type to instantiate

    Returns:
        Instance with default values
    """
    instance = cls_type()

    # Set all default values from class attributes
    for attr_name in dir(cls_type):
        if not attr_name.startswith('_') and hasattr(cls_type, attr_name):
            if not hasattr(cls_type, attr_name) or not isinstance(getattr(cls_type, attr_name), property):
                attr_value = getattr(cls_type, attr_name)
                setattr(instance, attr_name, attr_value)

    return instance


def config_class(**config_mapping: str) -> Callable[[T], T]:
    """Class decorator to mark a class as configurable with its own mapping.

    This decorator stores the configuration mapping in the class metadata
    for later use by map_config_to_class.

    Args:
        **config_mapping: Configuration mapping for this class

    Returns:
        Decorated class with stored configuration mapping

    Example:
        @config_class(
            ENABLED="auth.enabled",
            SECRET_KEY="auth.secret_key"
        )
        class AuthConfig:
            ENABLED: bool
            SECRET_KEY: str
    """
    def decorator(cls: T) -> T:
        # Store the configuration mapping in the class
        setattr(cls, '_config_mapping', config_mapping)
        return cls

    return decorator
