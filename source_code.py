import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt
from keras.preprocessing.image import ImageDataGenerator

def dehazing_module(input_shape=(256, 256, 3), filters=64):
    inputs = tf.keras.layers.Input(shape=input_shape)
    x = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(inputs)
    x = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(x)

    c1 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(x)
    c1 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(c1)

    c2 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(x)
    c2 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=2, padding="same", activation="relu")(c2)

    c3 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=2, padding="same", activation="relu")(x)
    c3 = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=2, padding="same", activation="relu")(c3)

    concatenated = tf.keras.layers.Concatenate(axis=-1)([c1, c2, c3])

    x = tf.keras.layers.Conv2D(filters, (3, 3), dilation_rate=1, padding="same", activation="relu")(concatenated)
    x = tf.keras.layers.Conv2D(filters, (1, 1), dilation_rate=1, padding="same", activation="relu")(x)

    omega = tf.keras.layers.Conv2D(3, (1, 1), padding='same', activation='sigmoid')(x)

    dehazed_image = tf.keras.layers.Lambda(lambda inputs: inputs[0] * inputs[1] - inputs[0] + 1)([omega, inputs])

    model = tf.keras.models.Model(inputs=inputs, outputs=dehazed_image)

    return model

# Load MobileNetV3-large backbone
def load_mobilenetv3_large():
    base_model = tf.keras.applications.MobileNetV3Large(
        input_shape=[None, None, 3],
        include_top=False,
        weights='imagenet'
    )
    base_model.trainable = False
    return base_model

# Define Faster-RCNN model
def create_faster_rcnn_model(num_classes):
    # Load MobileNetV3-large backbone
    backbone = load_mobilenetv3_large()

    # Create the Faster-RCNN model with MobileNetV3 backbone
    model = tf.keras.Sequential([
        tf.keras.layers.InputLayer(input_shape=[None, None, 3]),
        backbone,
        tf.keras.layers.GlobalAveragePooling2D(),
        tf.keras.layers.Dense(1024, activation='relu'),
        tf.keras.layers.Dense(num_classes, activation='softmax')
    ])
    
    return model

def hazy_image_feature(input_tensor):
    x = tf.keras.layers.Conv2D(32, (3, 3), padding = 'same')(input_tensor)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    hazy_feature = tf.keras.layers.Conv2D(32, (3, 3), padding = 'same')(x)

    return hazy_feature

def dehazy_image_feature(input_tensor):
    x = tf.keras.layers.Conv2D(32, (3, 3), padding = 'same')(input_tensor)
    x = tf.keras.layers.BatchNormalization()(x)
    x = tf.keras.layers.ReLU()(x)
    dehazing_feature = tf.keras.layers.Conv2D(32, (3, 3), padding = 'same')(x)

    return dehazing_feature

def hazy_aware_attention_fusion(hazy_feature, dehazing_feature, reduction_ratio=16):
    # Pointwise addition of hazed and dehazed features
    fused_feature = tf.keras.layers.Add()([hazy_feature, dehazing_feature])

    # fused features are average pooled along H and W
    pooled_w = tf.keras.layers.AveragePooling2D(pool_size=(1, fused_feature.shape[2]))(fused_feature)
    pooled_h = tf.keras.layers.AveragePooling2D(pool_size=(fused_feature.shape[1], 1))(fused_feature)

    # Reshape to make them compatible for concatenation
    pooled_h = tf.keras.layers.Permute((2, 1, 3))(pooled_h)  # Swap the height and width

    # pooled_w and pooled_h are spliced to get C x 1 x (H + W) features
    spliced_feature = tf.keras.layers.Concatenate(axis=2)([pooled_w, pooled_h])

    # Convolution, Batch Normalization and ReLU
    conv = tf.keras.layers.Conv2D(filters=fused_feature.shape[-1] // reduction_ratio, kernel_size=1, padding='valid')(spliced_feature)
    norm = tf.keras.layers.BatchNormalization()(conv)
    relu = tf.keras.layers.ReLU()(norm)

    # Use a Lambda layer to split the feature map
    height_feature, width_feature = tf.keras.layers.Lambda(lambda x: tf.split(x, num_or_size_splits=2, axis=2))(relu)

    # Convolution on split features
    height_feature = tf.keras.layers.Conv2D(filters=fused_feature.shape[-1], kernel_size=1)(height_feature)
    width_feature = tf.keras.layers.Conv2D(filters=fused_feature.shape[-1], kernel_size=1)(width_feature)

    # Matrix multiplication to get attention map
    attention_map = tf.keras.layers.Multiply()([height_feature, width_feature])
    attention_map = tf.keras.layers.Activation('sigmoid')(attention_map)

    # Compute the fused feature
    Fh = hazy_feature
    Fdeh = dehazing_feature

    # Use a Lambda layer to perform element-wise multiplication and addition
    fused_feature = tf.keras.layers.Lambda(
        lambda x: x[0] * x[2] + x[1] * (1 - x[2]))([Fh, Fdeh, attention_map])

    return fused_feature

def compute_hr_loss(Fh, Fdeh):
    # Global Average Pooling
    gap_Fh = tf.keras.layers.GlobalAveragePooling2D()(Fh)
    gap_Fdeh = tf.keras.layers.GlobalAveragePooling2D()(Fdeh)

    # Compute KL Divergence
    kl_divergence = tf.keras.losses.KLDivergence()(gap_Fh, gap_Fdeh)

    # Calculate HR Loss
    Lhr = tf.reduce_mean(kl_divergence)
    
    return Lhr

def compute_total_loss(Ldet, Fh, Fdeh, alpha=0.1):
    # Compute HR Loss
    Lhr = compute_hr_loss(Fh, Fdeh)

    # Total Loss
    Ltotal = Ldet + alpha * Lhr

    return Ltotal

def add_haze(dehazed_images, beta=0.1):
    depth_map = np.random.uniform(0.5, 2.0, dehazed_images.shape)
    A = 0.5
    hazy_images = dehazed_images * np.exp(-beta * depth_map) + A * (1 - np.exp(-beta * depth_map))
    return hazy_images



# Setup input shape and model
input_shape = (256, 256, 3)
hazy_input_tensor = tf.keras.layers.Input(shape=input_shape)

dehazing_model = dehazing_module(input_shape)
dehazed_image = dehazing_model(hazy_input_tensor)

hazy_features = hazy_image_feature(hazy_input_tensor)
dehazy_features = dehazy_image_feature(dehazed_image)

fused_features = hazy_aware_attention_fusion(hazy_features, dehazy_features)

Fh = hazy_features
Fdeh = dehazy_features

final_model = tf.keras.models.Model(inputs=hazy_input_tensor, outputs=fused_features)
final_model.compile(optimizer='adam', loss=lambda y_true, y_pred: compute_total_loss(y_true, y_pred, Fh, Fdeh, alpha=0.1))
final_model.summary()


# --------------------
# Load only hazy images from a directory with class subdirectories
hazy_images_directory = '/path/to/hazy_images'  # Update this path to your hazy images directory

# Define image dimensions and batch size
batch_size = 8
img_height, img_width = 256, 256

# Data generator for hazy images
hazy_datagen = ImageDataGenerator(
    rescale=1.0 / 255.0,
    validation_split=0.2  # Use 20% of images for validation
)

train_generator = hazy_datagen.flow_from_directory(
    hazy_images_directory,
    target_size=(img_height, img_width),
    batch_size=batch_size,
    class_mode='categorical',
    subset='training'  # Use this subset for training
)

validation_generator = hazy_datagen.flow_from_directory(
    hazy_images_directory,
    target_size=(img_height, img_width),
    batch_size=batch_size,
    class_mode='categorical',
    subset='validation'  # Use this subset for validation
)

# Batch the dataset
train_dataset = train_generator.batch(batch_size).prefetch(tf.data.experimental.AUTOTUNE)

# Iterate over the dataset to train the model
num_epochs = 10
for epoch in range(num_epochs):
    print(f"Epoch {epoch + 1}/{num_epochs}")

    if epoch % 2 == 0:
        print("Training on original hazy dataset...")
        final_model.fit(train_dataset, batch_size = 32, epochs=1, verbose=1)
    else:
        print("Training on refined hazy dataset...")
        refined_hazy_dataset = train_dataset.map(lambda x, y: (add_haze(x), y))
        final_model.fit(refined_hazy_dataset, epochs=1, verbose=1)